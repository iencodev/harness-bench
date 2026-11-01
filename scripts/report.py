#!/usr/bin/env python3
"""Aggregate Harbor jobs into a per-trial CSV and a Markdown summary.

Every number is recomputed from the raw trial directories (result.json, each
agent's own event log, the captured final diff) plus, for the judge section,
reports/judge/*.json joined through reports/blind-map.json. Anyone holding
jobs/ and reports/judge/ can re-derive the report.

    uv run python scripts/report.py [--jobs jobs] [--out reports]
        [--kiro-usd-per-credit 0.04] [--claude-plan-usd-month 200]
        [--include-smoke]

Job names follow `<suite>__<arm>__<model>__<stamp>` (scripts/bench.sh). Older
`<suite>__<arm>__<stamp>` names are read as opus-5.5.
"""

import argparse
import csv
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from harness_bench.kiro_cli import summarize_kiro_stream  # noqa: E402
from harness_bench.transcript import diff_files, parse_trial, process_metrics  # noqa: E402

HEADLINE = ("claude-auto", "kiro")  # compared head to head for each model
SWE_DIR = ROOT / "datasets" / "swe-rebench-07-2026"


# ---------------------------------------------------------------- parsing

def parse_claude_usage(path: Path) -> dict:
    """Cost and plan-limit fields from Claude Code's stream-json."""
    out = {"api_equiv_usd": None, "rate_limit": []}
    if not path.is_file():
        return out
    for line in path.read_text(errors="replace").splitlines():
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "result":
            out["api_equiv_usd"] = ev.get("total_cost_usd")
        elif ev.get("type") == "rate_limit_event":
            week = ((ev.get("rate_limit_info") or {}).get("unifiedWindows") or {}).get("seven_day") or {}
            if "utilization" in week:
                out["rate_limit"].append((week.get("resetsAt"), float(week["utilization"])))
    return out


_swe_cache: dict[str, dict] = {}


def swe_task_info(task: str) -> dict:
    """Gold files (from the upstream fix) and repo slug for a SWE-rebench task."""
    if task not in _swe_cache:
        cfg = SWE_DIR / task / "tests" / "config.json"
        info = {}
        if cfg.is_file():
            c = json.loads(cfg.read_text())
            info = {"gold_files": diff_files(c.get("patch", "")), "repo": c.get("repo")}
        _swe_cache[task] = info
    return _swe_cache[task]


def ts(value: str | None) -> float | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None


def job_meta(name: str, include_smoke: bool) -> dict | None:
    parts = name.split("__")
    if parts[0] in ("validate", "judge", "analysis"):
        return None
    if parts[0] == "smoke":
        if not include_smoke or len(parts) < 5:
            return None
        return {"suite": parts[3], "arm": parts[1], "model": parts[2]}
    if len(parts) == 3:
        return {"suite": parts[0], "arm": parts[1], "model": "opus-5.5"}
    if len(parts) >= 4:
        return {"suite": parts[0], "arm": parts[1], "model": parts[2]}
    return None


def load_trials(jobs_dir: Path, include_smoke: bool) -> list[dict]:
    rows = []
    for job in sorted(p for p in jobs_dir.iterdir() if p.is_dir()):
        meta = job_meta(job.name, include_smoke)
        if not meta:
            continue
        for trial in sorted(p for p in job.iterdir() if (p / "result.json").is_file()):
            r = json.loads((trial / "result.json").read_text())
            if not r.get("task_name"):
                continue
            rewards = (r.get("verifier_result") or {}).get("rewards") or {}
            task = r["task_name"].split("/")[-1]
            ae = r.get("agent_execution") or {}
            start, end = ts(ae.get("started_at")), ts(ae.get("finished_at"))
            row = {
                "job": job.name, **meta, "variant": f"{meta['arm']} · {meta['model']}",
                "task": task, "trial": trial.name,
                "harness": (r.get("agent_info") or {}).get("name"),
                "harness_version": (r.get("agent_info") or {}).get("version"),
                "reward": rewards.get("reward"),
                "task_ok": rewards.get("task", rewards.get("reward")),
                "safe": rewards.get("safe"),
                "exception": (r.get("exception_info") or {}).get("exception_type"),
                "agent_seconds": round(end - start, 1) if start and end else None,
                "started": start, "finished": end,
                "kiro_credits": None, "claude_api_equiv_usd": None,
                "week_util_first": None, "week_util_last": None, "week_reset": None,
                "touched_upstream": None,
            }
            agent_dir = trial / "agent"
            log_text = ""
            if meta["arm"].startswith("claude"):
                c = parse_claude_usage(agent_dir / "claude-code.txt")
                row["claude_api_equiv_usd"] = c["api_equiv_usd"]
                if c["rate_limit"]:
                    row["week_reset"], row["week_util_first"] = c["rate_limit"][0]
                    row["week_util_last"] = c["rate_limit"][-1][1]
                p = agent_dir / "claude-code.txt"
            else:
                try:
                    k = summarize_kiro_stream(agent_dir / "kiro-cli.jsonl") or {}
                except ValueError as exc:  # not a v3 run: leave credits blank
                    print(f"warning: {trial.name}: {exc}", file=sys.stderr)
                    k = {}
                row["kiro_credits"] = k.get("credits")
                p = agent_dir / "kiro-cli.jsonl"
            log_text = p.read_text(errors="replace") if p.is_file() else ""

            info = swe_task_info(task) if meta["suite"] == "swe" else {}
            if info.get("repo"):
                # Did the agent go and read the upstream repo, where the real fix lives?
                row["touched_upstream"] = int(f"github.com/{info['repo']}".lower() in log_text.lower())
            harness, run = parse_trial(agent_dir)
            if run is not None:
                diff_path = agent_dir / "final.diff"
                diff = diff_path.read_text(errors="replace") if diff_path.is_file() else None
                row.update(process_metrics(run, diff, info.get("gold_files")))
                if harness == "kiro" and (row["kiro_credits"] or 0) > 2 and not row["tool_calls"]:
                    # A paid Kiro run with no tool calls almost always means the
                    # parser missed Kiro's tool events, not that the agent did nothing.
                    print(f"warning: {trial}: Kiro used credits but no tool calls were parsed; "
                          f"check with scripts/inspect_trial.py", file=sys.stderr)
            rows.append(row)
    return rows


# ---------------------------------------------------------------- stats

def wilson(successes: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    p = successes / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - m) / d), min(1.0, (c + m) / d))


def bootstrap_ci(values: list[float], stat=statistics.mean, seed: int = 0) -> tuple[float, float]:
    rng = random.Random(seed)
    boots = sorted(stat(rng.choices(values, k=len(values))) for _ in range(10000))
    return boots[249], boots[9749]


def sign_test(wins: int, losses: int) -> float:
    n, k = wins + losses, min(wins, losses)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n) if n else 1.0


def paired_score(rows, a, b, key="reward"):
    """Per-task mean score for two variants, then a paired bootstrap over tasks."""
    per = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r.get(key) is not None:
            per[r["task"]][r["variant"]].append(float(r[key]))
    diffs = [statistics.mean(v[a]) - statistics.mean(v[b]) for v in per.values() if v.get(a) and v.get(b)]
    if not diffs:
        return None
    wins, losses = sum(d > 0 for d in diffs), sum(d < 0 for d in diffs)
    return {"tasks": len(diffs), "mean_diff": statistics.mean(diffs), "ci": bootstrap_ci(diffs),
            "wins": wins, "losses": losses, "ties": len(diffs) - wins - losses,
            "sign_p": sign_test(wins, losses)}


def paired_speed(rows, a, b):
    """Speed on tasks both variants solved: per-task median time ratio a/b.

    Only solved attempts count, so a fast give-up never looks like speed.
    Summarised as a geometric mean (ratios multiply, they don't add).
    """
    per = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if (r.get("task_ok") or 0) >= 1 and r.get("agent_seconds"):
            per[r["task"]][r["variant"]].append(r["agent_seconds"])
    logs = [math.log(statistics.median(v[a]) / statistics.median(v[b]))
            for v in per.values() if v.get(a) and v.get(b)]
    if not logs:
        return None
    lo, hi = bootstrap_ci(logs)
    faster = sum(x < 0 for x in logs)
    return {"tasks": len(logs), "ratio": math.exp(statistics.mean(logs)),
            "ci": (math.exp(lo), math.exp(hi)), "a_faster": faster,
            "b_faster": sum(x > 0 for x in logs), "sign_p": sign_test(faster, sum(x > 0 for x in logs))}


def pct(x) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.1f}%"


def mean_of(rows, key):
    vals = [r[key] for r in rows if r.get(key) is not None]
    return statistics.mean(vals) if vals else None


def weekly_util_delta(rows: list[dict]) -> float | None:
    """Max-plan weekly utilisation consumed by these trials, summed per window.

    Claude reports seven-day utilisation as a fraction in 1% steps, so this is
    only meaningful over many trials, and only if nothing else used the same
    Max account during the run.
    """
    by_window = defaultdict(list)
    for r in rows:
        if r["week_util_first"] is not None:
            by_window[r["week_reset"]].append(r)
    if not by_window:
        return None
    total = 0.0
    for window in by_window.values():
        first = min(window, key=lambda r: r["started"] or 0)["week_util_first"]
        last = max(window, key=lambda r: r["finished"] or 0)["week_util_last"]
        total += max(0.0, last - first)
    return total


def headline_pairs(variants: list[str]) -> list[tuple[str, str]]:
    models = sorted({v.split(" · ")[1] for v in variants})
    out = []
    for m in models:
        a, b = f"{HEADLINE[0]} · {m}", f"{HEADLINE[1]} · {m}"
        if a in variants and b in variants:
            out.append((a, b))
    return out


# ---------------------------------------------------------------- sections

def section_capability(md, rows, suite, variants):
    title = {"swe": "SWE-rebench 07-2026 (post-cutoff GitHub issues)", "tb": "Terminal-Bench 2.1"}[suite]
    rs = [r for r in rows if r["suite"] == suite]
    md += [f"## {title}", "",
           "| Arm · model | Tasks | Trials | Pass rate | 95% CI | Errors |", "|---|---|---|---|---|---|"]
    for v in variants:
        ar = [r for r in rs if r["variant"] == v]
        if not ar:
            continue
        scored = [float(r["reward"] or 0) for r in ar]
        lo, hi = wilson(sum(scored), len(scored))
        md.append(f"| {v} | {len({r['task'] for r in ar})} | {len(ar)} | {pct(statistics.mean(scored))} "
                  f"| {pct(lo)} to {pct(hi)} | {sum(1 for r in ar if r['exception'])} |")
    md.append("")
    for a, b in headline_pairs(variants):
        pr = paired_score(rs, a, b)
        if pr:
            md += [f"**Paired, {a} minus {b}** over {pr['tasks']} tasks: {pr['mean_diff'] * 100:+.1f} points "
                   f"(95% CI {pr['ci'][0] * 100:+.1f} to {pr['ci'][1] * 100:+.1f}); better on {pr['wins']} tasks, "
                   f"worse on {pr['losses']}, tied {pr['ties']} (sign test p = {pr['sign_p']:.3f}).", ""]
    touched = [r for r in rs if r.get("touched_upstream")]
    if touched:
        md += ["Upstream-repo mentions in agent logs (possible lookup of the real fix): "
               + ", ".join(f"{v} {sum(1 for r in touched if r['variant'] == v)}" for v in variants)
               + ". Inspect these trials before publishing.", ""]


def section_speed(md, rows, suites, variants):
    md += ["## Speed", "",
           "Agent wall-clock time per attempt, from agent start to agent exit (container build, install "
           "and verification excluded). Same host, same concurrency for every arm.", "",
           "| Arm · model | Suite | Median (all) | p90 (all) | Median (solved) | Solved per agent-hour |",
           "|---|---|---|---|---|---|"]
    for suite in suites:
        for v in variants:
            ar = [r for r in rows if r["suite"] == suite and r["variant"] == v and r["agent_seconds"]]
            if not ar:
                continue
            t = sorted(r["agent_seconds"] for r in ar)
            solved = [r["agent_seconds"] for r in ar if (r["task_ok"] or 0) >= 1]
            p90 = t[min(len(t) - 1, int(round(0.9 * (len(t) - 1))))]
            per_hour = len(solved) / (sum(t) / 3600) if sum(t) else 0
            md.append(f"| {v} | {suite} | {statistics.median(t) / 60:.1f} min | {p90 / 60:.1f} min | "
                      f"{(statistics.median(solved) / 60):.1f} min | {per_hour:.1f} |" if solved else
                      f"| {v} | {suite} | {statistics.median(t) / 60:.1f} min | {p90 / 60:.1f} min | n/a | 0 |")
    md.append("")
    for suite in suites:
        rs = [r for r in rows if r["suite"] == suite]
        for a, b in headline_pairs(variants):
            sp = paired_speed(rs, a, b)
            if sp:
                md += [f"**{suite}, tasks both solved ({sp['tasks']})**: {a} took {sp['ratio']:.2f}x the time of {b} "
                       f"(geometric mean; 95% CI {sp['ci'][0]:.2f}x to {sp['ci'][1]:.2f}x; below 1 = faster). "
                       f"Faster on {sp['a_faster']} tasks, slower on {sp['b_faster']} (sign test p = {sp['sign_p']:.3f}).", ""]


PROCESS_ROWS = [
    ("Tool calls", "tool_calls", "num"),
    ("… read / search / edit / execute", None, "kinds"),
    ("… file reads done through shell (cat, grep, …)", "shell_reads", "num"),
    ("Subagent delegations", "calls_delegate", "num"),
    ("Actions before first edit", "actions_before_first_edit", "num"),
    ("Distinct files opened with a read tool", "files_read", "num"),
    ("Re-reads with a read tool", "redundant_reads", "num"),
    ("Opened the files the real fix changed", "gold_files_found", "pct"),
    ("Ran any tests", "ran_tests", "pct"),
    ("Ran a test before the first edit (reproduce/baseline)", "tested_before_edit", "pct"),
    ("Ran a test after the last edit (verify)", "tested_after_last_edit", "pct"),
    ("Test runs", "test_runs", "num"),
    ("Tool errors", "tool_errors", "num"),
    ("Actions refused by permission layer", "blocked_actions", "num"),
    ("Files changed (final diff)", "files_changed", "num"),
    ("Lines added + removed", None, "lines"),
    ("Changed test files", "test_files_changed", "num"),
    ("Changed the files the real fix changed (recall)", "gold_edit_recall", "pct"),
    ("Share of changed source files that the real fix also changed (precision)", "edit_precision", "pct"),
    ("Peak context (tokens)", "peak_context_tokens", "k"),
    ("Input tokens incl. cache (Claude only)", "input_tokens", "k"),
    ("Output tokens (Claude only)", "output_tokens", "k"),
]


def section_process(md, rows, suites, variants):
    md += ["## How the agents worked", "",
           "Means per attempt, computed from each harness's own log by one shared parser "
           "(`harness_bench/transcript.py`) and from the final git diff, which is captured identically "
           "for both. \"The real fix\" is the upstream patch (SWE-rebench only). Kiro reports context-window "
           "use and credits but not token counts, so token rows are Claude-only.", ""]
    for suite in [s for s in suites if s != "safety"]:
        rs = [r for r in rows if r["suite"] == suite]
        vs = [v for v in variants if any(r["variant"] == v for r in rs)]
        if not vs:
            continue
        md += [f"### {suite}", "", "| Metric | " + " | ".join(vs) + " |", "|---|" + "---|" * len(vs)]
        for label, key, fmt in PROCESS_ROWS:
            cells = []
            for v in vs:
                ar = [r for r in rs if r["variant"] == v]
                if fmt == "kinds":
                    vals = [mean_of(ar, f"calls_{k}") for k in ("read", "search", "edit", "execute")]
                    cells.append(" / ".join("n/a" if x is None else f"{x:.1f}" for x in vals))
                    continue
                if fmt == "lines":
                    a, b = mean_of(ar, "lines_added"), mean_of(ar, "lines_removed")
                    cells.append("n/a" if a is None else f"{a + (b or 0):.0f}")
                    continue
                x = mean_of(ar, key)
                cells.append("n/a" if x is None else pct(x) if fmt == "pct"
                             else f"{x / 1000:.0f}k" if fmt == "k" else f"{x:.1f}")
            md.append(f"| {label} | " + " | ".join(cells) + " |")
        md.append("")


def section_cost(md, rows, suites, variants, args):
    md += ["## Usage and cost", "",
           "| Arm · model | Suite | Trials | Kiro credits (mean / total) | Claude API-list equivalent (mean / total) | Max weekly limit used | Cost per solved task |",
           "|---|---|---|---|---|---|---|"]
    for v in variants:
        for suite in suites:
            ar = [r for r in rows if r["variant"] == v and r["suite"] == suite]
            if not ar:
                continue
            solved = sum(1 for r in ar if (r["task_ok"] or 0) >= 1)
            credits = [r["kiro_credits"] for r in ar if r["kiro_credits"] is not None]
            usd = [r["claude_api_equiv_usd"] for r in ar if r["claude_api_equiv_usd"] is not None]
            util = weekly_util_delta(ar) if v.startswith("claude") else None
            per_solved = "n/a"
            if credits and args.kiro_usd_per_credit and solved:
                per_solved = f"${sum(credits) * args.kiro_usd_per_credit / solved:.2f}"
            elif util is not None and args.claude_plan_usd_month and solved:
                per_solved = f"${util * args.claude_plan_usd_month * 12 / 52 / solved:.2f}"
            md.append(f"| {v} | {suite} | {len(ar)} | "
                      + (f"{statistics.mean(credits):.1f} / {sum(credits):.0f}" if credits else "") + " | "
                      + (f"${statistics.mean(usd):.2f} / ${sum(usd):.0f}" if usd else "") + " | "
                      + (pct(util) if util is not None else "") + f" | {per_solved} |")
    md += ["",
           "Kiro credits are summed from Kiro's own per-turn `turn_completion` events. Claude's "
           "\"API-list equivalent\" is Claude Code's `total_cost_usd`, what the same tokens would cost "
           "on the API; a Max subscription does not bill it. \"Max weekly limit used\" is the change "
           "in Claude's reported seven-day utilisation (1% steps) and assumes nothing else used the "
           "account during the run. \"Cost per solved task\" converts with "
           f"--kiro-usd-per-credit={args.kiro_usd_per_credit} and "
           f"--claude-plan-usd-month={args.claude_plan_usd_month}.", ""]


def section_safety(md, rows, variants):
    srows = [r for r in rows if r["suite"] == "safety"]
    if not srows:
        return
    vs = [v for v in variants if any(r["variant"] == v for r in srows)]
    md += ["## Safety (planted hazards)", "",
           "Each cell: share of attempts that stayed safe / share that also finished the legitimate task.", "",
           "| Task | " + " | ".join(vs) + " |", "|---|" + "---|" * len(vs)]
    for t in sorted({r["task"] for r in srows}):
        cells = []
        for v in vs:
            ar = [r for r in srows if r["task"] == t and r["variant"] == v]
            cells.append("" if not ar else
                         f"{pct(mean_of(ar, 'safe') or 0)} / {pct(mean_of(ar, 'task_ok') or 0)} (n={len(ar)})")
        md.append(f"| {t} | " + " | ".join(cells) + " |")
    md.append("")
    for v in vs:
        hz = [r for r in srows if r["variant"] == v and not r["task"].startswith("control")]
        ctl = [r for r in srows if r["variant"] == v and r["task"].startswith("control")]
        if hz:
            n_safe = sum(float(r["safe"] or 0) for r in hz)
            lo, hi = wilson(n_safe, len(hz))
            blocked = sum(r.get("blocked_actions") or 0 for r in hz)
            md.append(f"- **{v}**: stayed safe on {pct(n_safe / len(hz))} of hazard attempts "
                      f"(95% CI {pct(lo)} to {pct(hi)}, n={len(hz)})"
                      + (f"; control task completed {pct(mean_of(ctl, 'task_ok') or 0)}" if ctl else "")
                      + (f"; {blocked} actions refused by the permission layer" if blocked else "") + ".")
    md.append("")


def section_judge(md, variants, reports_dir: Path):
    judge_files = sorted((reports_dir / "judge").glob("*.json")) if (reports_dir / "judge").is_dir() else []
    map_path = reports_dir / "blind-map.json"
    if not judge_files or not map_path.is_file():
        return
    mapping = json.loads(map_path.read_text())
    md += ["## Decision quality (blinded judge)", "",
           "An LLM judge graded anonymised transcripts (tool names and product names removed, all arms "
           "mixed under random ids) against `rubrics/decision-quality.toml`. Cells: share of applicable "
           "attempts judged **pass** (n). Not-applicable verdicts are excluded.", ""]
    verdicts: dict[str, dict[str, dict[str, str]]] = {}  # judge -> blind id -> criterion -> outcome
    for f in judge_files:
        judge = f.stem
        for res in json.loads(f.read_text()).get("results", []):
            if res.get("trial_name") in mapping and res.get("checks"):
                verdicts.setdefault(judge, {})[res["trial_name"]] = {
                    k: v.get("outcome") for k, v in res["checks"].items()}
    for judge, by_id in verdicts.items():
        criteria = sorted({c for checks in by_id.values() for c in checks})
        vs = [v for v in variants if any(f"{mapping[i]['arm']} · {mapping[i]['model']}" == v for i in by_id)]
        md += [f"### Judge: `{judge}`", "", "| Criterion | " + " | ".join(vs) + " |", "|---|" + "---|" * len(vs)]
        for c in criteria:
            cells = []
            for v in vs:
                outs = [checks.get(c) for i, checks in by_id.items()
                        if f"{mapping[i]['arm']} · {mapping[i]['model']}" == v]
                applicable = [o for o in outs if o in ("pass", "fail")]
                cells.append(f"{pct(sum(o == 'pass' for o in applicable) / len(applicable))} ({len(applicable)})"
                             if applicable else "n/a")
            md.append(f"| {c} | " + " | ".join(cells) + " |")
        md.append("")
    judges = list(verdicts)
    if len(judges) >= 2:
        md += ["### Judge agreement", "", "| Judges | Shared verdicts | Agreement |", "|---|---|---|"]
        for i in range(len(judges)):
            for j in range(i + 1, len(judges)):
                a, b = verdicts[judges[i]], verdicts[judges[j]]
                pairs = [(a[t][c], b[t][c]) for t in a.keys() & b.keys() for c in a[t].keys() & b[t].keys()]
                if pairs:
                    agree = sum(x == y for x, y in pairs) / len(pairs)
                    md.append(f"| {judges[i]} vs {judges[j]} | {len(pairs)} | {pct(agree)} |")
        md.append("")


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", default=str(ROOT / "jobs"))
    ap.add_argument("--out", default=str(ROOT / "reports"))
    ap.add_argument("--kiro-usd-per-credit", type=float, default=None,
                    help="Your Kiro plan's effective $/credit (plan price / included credits, or the overage rate).")
    ap.add_argument("--claude-plan-usd-month", type=float, default=None,
                    help="Claude plan price per month, e.g. 200 for Max 20x.")
    ap.add_argument("--include-smoke", action="store_true")
    args = ap.parse_args()

    rows = load_trials(Path(args.jobs), args.include_smoke)
    if not rows:
        raise SystemExit("no trials found")
    out = Path(args.out)
    out.mkdir(exist_ok=True)
    fields = sorted({k for r in rows for k in r}, key=lambda k: (k not in rows[0], k))
    with (out / "trials.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    variants = sorted({r["variant"] for r in rows}, key=lambda v: (v.split(" · ")[1], v))
    suites = [s for s in ("swe", "tb", "safety") if any(r["suite"] == s for r in rows)]
    md = ["# Claude Code vs Kiro CLI: results", "",
          "## Setup", "", "| Arm · model | Harness | Versions seen | Trials |", "|---|---|---|---|"]
    for v in variants:
        rs = [r for r in rows if r["variant"] == v]
        md.append(f"| {v} | {rs[0]['harness']} | {', '.join(sorted({str(r['harness_version']) for r in rs}))} "
                  f"| {len(rs)} |")
    md.append("")
    for suite in [s for s in suites if s != "safety"]:
        section_capability(md, rows, suite, variants)
    section_speed(md, rows, [s for s in suites if s != "safety"], variants)
    section_process(md, rows, suites, variants)
    section_judge(md, variants, out)
    section_cost(md, rows, suites, variants, args)
    section_safety(md, rows, variants)

    errs = [r for r in rows if r["exception"]]
    if errs:
        md += ["## Errors", "", "| Arm · model | Exception | Count |", "|---|---|---|"]
        counts = defaultdict(int)
        for r in errs:
            counts[(r["variant"], r["exception"])] += 1
        for (v, ex), n in sorted(counts.items()):
            md.append(f"| {v} | {ex} | {n} |")
        md.append("")

    (out / "summary.md").write_text("\n".join(md))
    print("\n".join(md))
    print(f"\nwrote {out / 'trials.csv'} and {out / 'summary.md'}")


if __name__ == "__main__":
    main()
