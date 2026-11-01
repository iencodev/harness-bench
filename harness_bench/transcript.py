"""One normalised view of what an agent did, built from either harness's log.

Claude Code and Kiro log in different formats (Claude's stream-json; Kiro's
ACP JSON Lines). Every process metric in the report, and every transcript the
blinded judge reads, goes through this module, so both harnesses are measured
by the same code.

An agent run becomes a list of `Event`s:

    kind      read | search | edit | execute | fetch | delegate | plan | other | message
    path      file the action touched, when there is one
    command   shell command, for kind == execute
    shell     test | build | read | vcs | other  (what an execute command does)
    error     the tool reported failure
    text      assistant prose (kind == message) or a short action summary
    output    tool output, truncated
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

KIRO_CONTEXT_WINDOW = 1_000_000  # Kiro lists Opus/Sonnet/Haiku 5.5 with a 1M window

# ------------------------------------------------------------------ events


@dataclass
class Event:
    kind: str
    path: str | None = None
    command: str | None = None
    shell: str | None = None
    error: bool = False
    blocked: bool = False  # refused by the harness's permission layer, not a failed action
    text: str = ""
    output: str = ""


@dataclass
class Run:
    events: list[Event] = field(default_factory=list)
    # Token and context accounting. Claude reports tokens per model call;
    # Kiro reports context-window use (%) and billed credits, not tokens.
    input_tokens: int | None = None  # incl. cache reads and writes
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    peak_context_tokens: int | None = None
    model_calls: int | None = None


# What an execute command does. The same patterns apply to both harnesses.
_TEST = re.compile(
    r"\b(pytest|py\.test|unittest|tox|nox|go\s+test|cargo\s+(test|nextest)|"
    r"mvnw?\b[^|;&]*\b(test|verify)\b|gradlew?\b[^|;&]*\btest\b|"
    r"(npm|pnpm|yarn|bun)\s+(run\s+)?test|npx\s+(vitest|jest|mocha)|vitest|jest|mocha|"
    r"ctest|make\s+(test|check)|dotnet\s+test|deno\s+test|phpunit|rspec)\b"
)
_BUILD = re.compile(
    r"\b(cargo\s+(build|check|clippy)|go\s+(build|vet)|mvnw?\b[^|;&]*\b(compile|package)\b|"
    r"gradlew?\b[^|;&]*\b(build|compile\w*)\b|(npm|pnpm|yarn)\s+(run\s+)?(build|lint|typecheck)|"
    r"\btsc\b|make\b(?!\s+(test|check))|python\s+-m\s+(compileall|py_compile)|ruff|mypy|eslint)\b"
)
_READ = re.compile(
    r"^\s*(cat|head|tail|less|more|nl|wc|ls|tree|find|grep|egrep|rg|ag|fd|sed\s+-n|awk|"
    r"git\s+(log|show|diff|status|blame|grep|ls-files))\b"
)
# Shell commands that change files: sed -i, perl -pi, patch/git apply, or a
# redirect into a real file (not 2>&1, not /dev/null, not /tmp scratch).
_SHELL_WRITE = re.compile(
    r"\bsed\s+(-\w*\s+)*-i|\bperl\s+-\w*i|\bpatch\b|\bgit\s+apply\b|"
    # scripted edits: python/node writing a file
    r"open\([^)]*,\s*['\"][wa]\+?['\"]|\.write_text\(|writeFileSync\(|"
    r"(?<![\d&>])>>?\s*(?!&|/dev/null|/tmp/)[\w./~-]"
)
# How a permission refusal reads in a tool result (auto-mode classifier,
# denied rules). Counted as `blocked`, not as a tool error.
_BLOCKED = re.compile(r"denied by|permission (was )?denied|requires approval|not allowed to run|classifier", re.I)
_VCS = re.compile(r"\bgit\s+(commit|push|pull|checkout|switch|reset|rebase|stash|merge|apply|am)\b")


def classify_command(command: str) -> str:
    if _TEST.search(command):
        return "test"
    if _BUILD.search(command):
        return "build"
    if _VCS.search(command):
        return "vcs"
    # Skip setup segments (cd, export, source, set) to find the real first command.
    segments = [x for x in re.split(r"\s*(?:&&|\|\||;|\||\n)\s*", command.strip()) if x]
    for seg in segments:
        if not re.match(r"(cd|pushd|export|source|\.|set|unset)\b", seg):
            return "read" if _READ.search(seg) else "other"
    return "other"


def _truncate(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        if isinstance(value, list):  # content blocks
            value = "\n".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in value
            )
        else:
            value = json.dumps(value, ensure_ascii=False)
    return value if len(value) <= limit else value[:limit] + f"… [{len(value) - limit} chars cut]"


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


# ------------------------------------------------------------------ Claude Code

_CLAUDE_KIND = {
    "Read": "read", "NotebookRead": "read",
    "Grep": "search", "Glob": "search", "LS": "search", "ToolSearch": "other",
    "Edit": "edit", "MultiEdit": "edit", "Write": "edit", "NotebookEdit": "edit",
    "Bash": "execute", "BashOutput": "execute", "KillShell": "execute", "Monitor": "execute",
    "WebFetch": "fetch", "WebSearch": "fetch",
    "Agent": "delegate", "Task": "delegate",
    "TodoWrite": "plan", "TaskCreate": "plan", "TaskUpdate": "plan", "ExitPlanMode": "plan",
}


def parse_claude(path: Path, output_limit: int = 2000) -> Run:
    run = Run(input_tokens=0, output_tokens=0, cache_read_tokens=0, peak_context_tokens=0)
    by_id: dict[str, Event] = {}
    usage_by_message: dict[str, dict] = {}
    final_usage: dict | None = None
    for ev in _read_jsonl(path):
        msg = ev.get("message") or {}
        if ev.get("type") == "result":
            final_usage = ev.get("usage")
        if ev.get("type") == "assistant":
            if msg.get("id") and msg.get("usage"):
                # stream-json repeats a message's usage on each content block; keep one.
                usage_by_message[msg["id"]] = msg["usage"]
            for block in msg.get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    run.events.append(Event("message", text=_truncate(block["text"], output_limit)))
                elif block.get("type") == "tool_use":
                    name, inp = block.get("name", ""), block.get("input") or {}
                    e = Event(_CLAUDE_KIND.get(name, "other"))
                    e.path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path")
                    if e.kind == "execute":
                        e.command = inp.get("command")
                        e.shell = classify_command(e.command or "")
                    elif e.kind == "search":
                        e.text = f"pattern={inp.get('pattern')!r}"
                    elif e.kind in ("fetch", "delegate"):
                        e.text = _truncate(inp.get("url") or inp.get("query") or inp.get("prompt")
                                           or inp.get("description"), 300)
                    elif e.kind == "edit":
                        e.text = _truncate(
                            {k: inp.get(k) for k in ("old_string", "new_string", "content") if k in inp},
                            output_limit)
                    by_id[block.get("id")] = e
                    run.events.append(e)
        elif ev.get("type") == "user":
            content = msg.get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result" \
                        and block.get("tool_use_id") in by_id:
                    e = by_id[block["tool_use_id"]]
                    e.error = bool(block.get("is_error"))
                    e.output = _truncate(block.get("content"), output_limit)
                    if e.error and _BLOCKED.search(e.output[:400]):
                        e.blocked, e.error = True, False
    # Per-message usage in stream-json is a snapshot from the start of the
    # response (output tokens undercounted), so it only gives context size.
    # Totals come from the final `result` event, which Claude Code sums itself.
    for u in usage_by_message.values():
        context = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
            + (u.get("cache_creation_input_tokens") or 0)
        run.peak_context_tokens = max(run.peak_context_tokens, context)
    run.model_calls = len(usage_by_message)
    if final_usage:
        run.cache_read_tokens = final_usage.get("cache_read_input_tokens") or 0
        run.input_tokens = (final_usage.get("input_tokens") or 0) + run.cache_read_tokens \
            + (final_usage.get("cache_creation_input_tokens") or 0)
        run.output_tokens = final_usage.get("output_tokens") or 0
    else:  # run cut off before the result event (e.g. timeout): totals unknown
        run.input_tokens = run.output_tokens = run.cache_read_tokens = None
    return run


# ------------------------------------------------------------------ Kiro (ACP)

_KIRO_KIND = {
    "read": "read", "search": "search", "edit": "edit", "delete": "edit", "move": "edit",
    "execute": "execute", "fetch": "fetch", "think": "plan",
}


def _first_str(d: Any, keys: tuple[str, ...]) -> str | None:
    if isinstance(d, dict):
        for k in keys:
            if isinstance(d.get(k), str) and d[k]:
                return d[k]
    return None


def parse_kiro(path: Path, output_limit: int = 2000) -> Run:
    """Read Kiro v3 stream-json (ACP session updates).

    ACP tool calls carry a `kind` (read, edit, delete, move, search, execute,
    think, fetch, other) defined by the protocol, which maps directly onto the
    normalised kinds. Kiro does not report token counts; peak context comes
    from its context_usage percentage.
    """
    run = Run()
    by_id: dict[str, Event] = {}
    text_buf: list[str] = []
    peak_pct = 0.0

    def flush_text():
        if text_buf and "".join(text_buf).strip():
            run.events.append(Event("message", text=_truncate("".join(text_buf), output_limit)))
        text_buf.clear()

    for ev in _read_jsonl(path):
        update = (ev.get("data") or {}).get("update") or {}
        su = update.get("sessionUpdate")
        if su == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text":
                text_buf.append(content.get("text", ""))
            continue
        if su in ("tool_call", "tool_call_update"):
            flush_text()
            tid = update.get("toolCallId")
            e = by_id.get(tid)
            if e is None:
                title = update.get("title") or ""
                kind = _KIRO_KIND.get(update.get("kind") or "", "other")
                if kind == "other" and re.search(r"agent|delegat|gatherer", title, re.I):
                    kind = "delegate"
                e = Event(kind, text=_truncate(title, 300))
                by_id[tid] = e
                run.events.append(e)
            raw = update.get("rawInput")
            if raw:
                locs = update.get("locations") or []
                e.path = e.path or (locs[0].get("path") if locs and isinstance(locs[0], dict) else None) \
                    or _first_str(raw, ("path", "file_path", "filePath", "file"))
                if e.kind == "execute":
                    e.command = e.command or _first_str(raw, ("command", "cmd", "script")) or e.text
                    e.shell = classify_command(e.command or "")
                elif e.kind == "edit":
                    e.text = _truncate(raw, output_limit)
            elif update.get("locations") and not e.path:
                loc = update["locations"][0]
                e.path = loc.get("path") if isinstance(loc, dict) else None
            if e.kind == "execute" and not e.command and e.text:
                e.command, e.shell = e.text, classify_command(e.text)
            status = update.get("status")
            if status == "failed":
                e.error = True
            out = update.get("rawOutput") or update.get("content")
            if out and status in ("completed", "failed"):
                e.output = _truncate(out, output_limit)
            continue
        meta = (update.get("_meta") or {}).get("kiro") or {}
        usage = meta.get("contextUsage") or {}
        if isinstance(usage.get("usagePercentage"), (int, float)):
            peak_pct = max(peak_pct, float(usage["usagePercentage"]))
    flush_text()
    run.peak_context_tokens = int(peak_pct / 100 * KIRO_CONTEXT_WINDOW) if peak_pct else None
    return run


def parse_trial(agent_dir: Path) -> tuple[str | None, Run | None]:
    """Find whichever harness log a trial has and parse it."""
    if (agent_dir / "claude-code.txt").is_file():
        return "claude", parse_claude(agent_dir / "claude-code.txt")
    if (agent_dir / "kiro-cli.jsonl").is_file():
        return "kiro", parse_kiro(agent_dir / "kiro-cli.jsonl")
    return None, None


# ------------------------------------------------------------------ diffs

_DIFF_FILE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.M)
# Build and test by-products that show up as new untracked files. They say
# nothing about the agent's change, so diff metrics skip them.
_GENERATED = re.compile(
    r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|node_modules|\.gradle|"
    r"target|build|dist|out|bin|obj|\.next|coverage|\.tox|\.venv|venv)/|"
    r"\.(pyc|pyo|class|o|so|a|dll|exe|jar|log)$"
)


def _blocks(diff_text: str) -> list[tuple[str, str]]:
    """Split a diff into (path, block) pairs, dropping generated files."""
    starts = [m for m in _DIFF_FILE.finditer(diff_text or "")]
    out = []
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(diff_text)
        if not _GENERATED.search(m.group(2)):
            out.append((m.group(2), diff_text[m.start():end]))
    return out


def diff_files(diff_text: str) -> list[str]:
    return sorted({path for path, _ in _blocks(diff_text)})


def diff_size(diff_text: str) -> tuple[int, int]:
    added = removed = 0
    for _, block in _blocks(diff_text):
        for line in block.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
    return added, removed


_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|testdata)/|(^|/)test_|_test\.|\.test\.|\.spec\.|Tests?\.\w+$")


def is_test_path(p: str) -> bool:
    return bool(_TEST_PATH.search(p))


def same_file(agent_path: str, repo_path: str) -> bool:
    """Agent paths are often absolute (/testbed/src/x.rs); gold paths are repo-relative."""
    a = agent_path.replace("\\", "/")
    return a == repo_path or a.endswith("/" + repo_path)


# ------------------------------------------------------------------ metrics


def process_metrics(run: Run, final_diff: str | None, gold_files: list[str] | None) -> dict:
    """Harness-neutral process metrics for one trial."""
    ev = [e for e in run.events if e.kind != "message"]
    kinds = {k: sum(1 for e in ev if e.kind == k) for k in
             ("read", "search", "edit", "execute", "fetch", "delegate", "plan", "other")}
    tests = [i for i, e in enumerate(ev) if e.kind == "execute" and e.shell == "test"]
    edits = [i for i, e in enumerate(ev) if e.kind == "edit"
             or (e.kind == "execute" and e.command and _SHELL_WRITE.search(e.command))]
    final = diff_files(final_diff) if final_diff is not None else None
    kept_edits = [i for i in edits if final is None or not ev[i].path
                  or ev[i].kind != "edit" or any(same_file(ev[i].path, f) for f in final)]
    reads = [e.path for e in ev if e.kind == "read" and e.path]
    shell_reads = sum(1 for e in ev if e.kind == "execute" and e.shell == "read")
    m: dict[str, Any] = {
        "tool_calls": len(ev),
        **{f"calls_{k}": v for k, v in kinds.items()},
        "shell_reads": shell_reads,
        "tool_errors": sum(1 for e in ev if e.error),
        "blocked_actions": sum(1 for e in ev if e.blocked),
        "test_runs": len(tests),
        "ran_tests": int(bool(tests)),
        # Ran a test before the first change: reproduced or baselined the failure.
        "tested_before_edit": int(bool(tests and edits and tests[0] < edits[0])),
        # Ran a test after the last change: verified the final state. A command
        # that edits and then tests (`sed -i ... && go test`) counts, and edits
        # to scratch files absent from the final diff (a deleted temp test) don't.
        "tested_after_last_edit": int(bool(tests and kept_edits and tests[-1] >= kept_edits[-1])),
        "files_read": len(set(reads)),
        "redundant_reads": len(reads) - len(set(reads)),
        "actions_before_first_edit": edits[0] if edits else len(ev),
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "cache_read_tokens": run.cache_read_tokens,
        "peak_context_tokens": run.peak_context_tokens,
        "model_calls": run.model_calls,
    }
    if final_diff is not None:
        changed = diff_files(final_diff)
        added, removed = diff_size(final_diff)
        m.update(files_changed=len(changed), lines_added=added, lines_removed=removed,
                 test_files_changed=sum(1 for f in changed if is_test_path(f)))
    if gold_files:
        src_gold = [g for g in gold_files if not is_test_path(g)] or gold_files
        looked = {e.path for e in ev if e.path and e.kind in ("read", "edit", "search")}
        cmds = " ".join(e.command or "" for e in ev if e.kind == "execute")
        # Retrieval: did the agent open the files the real fix changed?
        found = [g for g in src_gold
                 if any(same_file(p, g) for p in looked) or g in cmds or g.rsplit("/", 1)[-1] in cmds]
        m["gold_files_found"] = round(len(found) / len(src_gold), 3)
        if final_diff is not None:
            changed_src = [f for f in diff_files(final_diff) if not is_test_path(f)]
            hit = [f for f in changed_src if f in src_gold]
            # Did it change the files the real fix changed, and only those?
            m["gold_edit_recall"] = round(len(set(hit)) / len(src_gold), 3)
            m["edit_precision"] = round(len(hit) / len(changed_src), 3) if changed_src else None
    return m


# Product names and harness-specific files that would tell the judge which
# harness ran. "Claude" alone stays: both harnesses run a Claude model.
_IDENTIFYING = re.compile(
    r"claude[\s_-]*code|kiro[\w.-]*|amazon\s+q\b|CLAUDE\.md|AGENTS\.md|\.claude/[\w./-]*|"
    r"auto[\s-]mode classifier|context-gatherer|general-task-execution",
    re.I,
)


def _redact(text: str) -> str:
    return _IDENTIFYING.sub("[harness]", text)


def neutral_transcript(run: Run) -> list[dict]:
    """Harness-blind transcript for the judge: kinds, not tool names."""
    out = []
    for i, e in enumerate(run.events, 1):
        d = {k: v for k, v in asdict(e).items() if v not in (None, "", False)}
        if e.blocked:
            # The refusal wording is product-specific; keep only the fact.
            d["output"] = "[action refused by the agent's permission layer]"
        for k in ("text", "output", "command", "path"):
            if isinstance(d.get(k), str):
                d[k] = _redact(d[k])
        d["step"] = i
        out.append(d)
    return out
