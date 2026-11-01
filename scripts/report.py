#!/usr/bin/env python3
"""Aggregate Harbor jobs into a per-trial CSV and a Markdown summary.

Everything is recomputed from the raw trial directories (result.json plus each
agent's own event log), so anyone holding the jobs/ folder can re-derive every
number in the report.

    uv run python scripts/report.py [--jobs jobs] [--out reports]
        [--kiro-usd-per-credit 0.04] [--claude-plan-usd-month 200]
        [--include-smoke]

Job names follow `<suite>__<arm>__<stamp>` (scripts/bench.sh sets them).
"""

import argparse
import csv
import json
import math
import random
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from harness_bench.kiro_cli import summarize_kiro_stream  # noqa: E402

REFERENCE_PAIR = ("claude-auto", "kiro")  # the headline comparison


# ---------------------------------------------------------------- parsing

def parse_claude_log(path: Path) -> dict:
    out = {"turns": None, "api_equiv_usd": None, "permission_denials": None,
           "rate_limit": []}
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
            out["turns"] = ev.get("num_turns")
            out["api_equiv_usd"] = ev.get("total_cost_usd")
            # In auto mode these are the classifier's blocks.
            out["permission_denials"] = len(ev.get("permission_denials") or [])
        elif ev.get("type") == "rate_limit_event":
            windows = (ev.get("rate_limit_info") or {}).get("unifiedWindows") or {}
            week = windows.get("seven_day") or {}
            if "utilization" in week:
                out["rate_limit"].append((week.get("resetsAt"), float(week["utilization"])))
    return out


def upstream_slug(task: str) -> str | None:
    # SWE-rebench task ids look like owner__repo-1234.
    m = re.fullmatch(r"(.+?)__(.+)-\d+", task)
    return f"github.com/{m.group(1)}/{m.group(2)}" if m else None


def ts(value: str | None) -> float | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None


def load_trials(jobs_dir: Path, include_smoke: bool) -> list[dict]:
    rows = []
    for job in sorted(p for p in jobs_dir.iterdir() if p.is_dir()):
        parts = job.name.split("__")
        if parts[0] == "validate" or (parts[0] == "smoke" and not include_smoke):
            continue
        if parts[0] == "smoke":
            suite, arm = parts[2], parts[1]
        elif len(parts) >= 3:
            suite, arm = parts[0], parts[1]
        else:
            continue
        for trial in sorted(p for p in job.iterdir() if (p / "result.json").is_file()):
            r = json.loads((trial / "result.json").read_text())
            if not r.get("task_name"):
                continue
            rewards = (r.get("verifier_result") or {}).get("rewards") or {}
            task = r["task_name"].split("/")[-1]
            ex = r.get("exception_info") or {}
            ae = r.get("agent_execution") or {}
            start, end = ts(ae.get("started_at")), ts(ae.get("finished_at"))
            row = {
                "job": job.name, "suite": suite, "arm": arm, "task": task,
                "trial": trial.name,
                "harness": (r.get("agent_info") or {}).get("name"),
                "harness_version": (r.get("agent_info") or {}).get("version"),
                "reward": rewards.get("reward"),
                "task_ok": rewards.get("task", rewards.get("reward")),
                "safe": rewards.get("safe"),
                "exception": ex.get("exception_type"),
                "agent_seconds": round(end - start, 1) if start and end else None,
                "started": start, "finished": end,
                "turns": None, "kiro_credits": None, "claude_api_equiv_usd": None,
                "claude_denials": None, "week_util_first": None, "week_util_last": None,
                "week_reset": None, "touched_upstream": None,
            }
            agent_dir = trial / "agent"
            log_text = ""
            if row["arm"].startswith("claude"):
                c = parse_claude_log(agent_dir / "claude-code.txt")
                row.update(turns=c["turns"], claude_api_equiv_usd=c["api_equiv_usd"],
                           claude_denials=c["permission_denials"])
                if c["rate_limit"]:
                    row["week_reset"], row["week_util_first"] = c["rate_limit"][0]
                    row["week_util_last"] = c["rate_limit"][-1][1]
                p = agent_dir / "claude-code.txt"
                log_text = p.read_text(errors="replace") if p.is_file() else ""
            elif row["arm"].startswith("kiro"):
                try:
                    k = summarize_kiro_stream(agent_dir / "kiro-cli.jsonl") or {}
                except ValueError as exc:  # not a v3 run: leave credits blank
                    print(f"warning: {trial.name}: {exc}", file=sys.stderr)
                    k = {}
                row.update(turns=k.get("turns"), kiro_credits=k.get("credits"))
                p = agent_dir / "kiro-cli.jsonl"
                log_text = p.read_text(errors="replace") if p.is_file() else ""
            slug = upstream_slug(task) if suite == "swe" else None
            if slug:
                # Did the agent go and read the upstream repo (where the real fix lives)?
                row["touched_upstream"] = int(slug.lower() in log_text.lower())
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


def paired(rows: list[dict], a: str, b: str, key: str = "reward", seed: int = 0):
    """Per-task mean score for each arm, then a paired bootstrap over tasks."""
    per = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r[key] is not None:
            per[r["task"]][r["arm"]].append(float(r[key]))
    diffs = [statistics.mean(v[a]) - statistics.mean(v[b])
             for v in per.values() if v.get(a) and v.get(b)]
    if not diffs:
        return None
    rng = random.Random(seed)
    boots = sorted(statistics.mean(rng.choices(diffs, k=len(diffs))) for _ in range(10000))
    wins = sum(d > 0 for d in diffs)
    losses = sum(d < 0 for d in diffs)
    # Two-sided exact sign test on tasks that differ.
    n = wins + losses
    k = min(wins, losses)
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n) if n else 1.0
    return {"tasks": len(diffs), "mean_diff": statistics.mean(diffs),
            "ci": (boots[249], boots[9749]), "wins": wins, "losses": losses,
            "ties": len(diffs) - n, "sign_p": p}


def fmt_pct(x) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.1f}%"


def weekly_util_delta(rows: list[dict]) -> float | None:
    """Plan utilisation consumed by these trials, summed per weekly window.

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


# ---------------------------------------------------------------- report

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
    with (out / "trials.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    md: list[str] = ["# Claude Code vs Kiro CLI: results", ""]
    arms = sorted({r["arm"] for r in rows})
    suites = [s for s in ("swe", "tb", "safety") if any(r["suite"] == s for r in rows)]

    md += ["## Setup", "", "| Arm | Harness | Versions seen | Trials |", "|---|---|---|---|"]
    for arm in arms:
        rs = [r for r in rows if r["arm"] == arm]
        versions = ", ".join(sorted({str(r["harness_version"]) for r in rs}))
        md.append(f"| {arm} | {rs[0]['harness']} | {versions} | {len(rs)} |")
    md.append("")

    for suite in [s for s in suites if s != "safety"]:
        title = {"swe": "SWE-rebench 07-2026 (post-cutoff GitHub issues)",
                 "tb": "Terminal-Bench 2.1"}[suite]
        rs = [r for r in rows if r["suite"] == suite]
        md += [f"## {title}", "",
               "| Arm | Tasks | Trials | Pass rate | 95% CI | Errors | Median agent time | Mean turns |",
               "|---|---|---|---|---|---|---|---|"]
        for arm in arms:
            ar = [r for r in rs if r["arm"] == arm]
            if not ar:
                continue
            scored = [float(r["reward"] or 0) for r in ar]
            lo, hi = wilson(sum(scored), len(scored))
            times = [r["agent_seconds"] for r in ar if r["agent_seconds"]]
            turns = [r["turns"] for r in ar if r["turns"]]
            med_time = f"{statistics.median(times) / 60:.1f} min" if times else "n/a"
            mean_turns = f"{statistics.mean(turns):.1f}" if turns else "n/a"
            errors = sum(1 for r in ar if r["exception"])
            md.append(
                f"| {arm} | {len({r['task'] for r in ar})} | {len(ar)} "
                f"| {fmt_pct(statistics.mean(scored))} | {fmt_pct(lo)} to {fmt_pct(hi)} "
                f"| {errors} | {med_time} | {mean_turns} |")
        md.append("")
        a, b = REFERENCE_PAIR
        pr = paired(rs, a, b)
        if pr:
            md += [f"**Paired, {a} minus {b}** over {pr['tasks']} tasks: "
                   f"{pr['mean_diff'] * 100:+.1f} points (95% bootstrap CI "
                   f"{pr['ci'][0] * 100:+.1f} to {pr['ci'][1] * 100:+.1f}); "
                   f"{a} better on {pr['wins']} tasks, {b} better on {pr['losses']}, tied {pr['ties']} "
                   f"(sign test p = {pr['sign_p']:.3f}).", ""]
        if suite == "swe":
            touched = [r for r in rs if r["touched_upstream"]]
            if touched:
                md += [f"Upstream-repo mentions in agent logs (possible lookup of the real fix): "
                       + ", ".join(f"{arm} {sum(1 for r in touched if r['arm'] == arm)}" for arm in arms)
                       + ". Inspect these trials before publishing.", ""]

    # Cost
    md += ["## Usage and cost", "",
           "| Arm | Suite | Trials | Kiro credits (mean / total) | Claude API-list equivalent (mean / total) | Max weekly limit used | Cost per solved task |",
           "|---|---|---|---|---|---|---|"]
    for arm in arms:
        for suite in suites:
            ar = [r for r in rows if r["arm"] == arm and r["suite"] == suite]
            if not ar:
                continue
            solved = sum(1 for r in ar if (r["task_ok"] or 0) >= 1)
            credits = [r["kiro_credits"] for r in ar if r["kiro_credits"] is not None]
            usd = [r["claude_api_equiv_usd"] for r in ar if r["claude_api_equiv_usd"] is not None]
            util = weekly_util_delta(ar) if arm.startswith("claude") else None
            per_solved = "n/a"
            if credits and args.kiro_usd_per_credit and solved:
                per_solved = f"${sum(credits) * args.kiro_usd_per_credit / solved:.2f}"
            elif util is not None and args.claude_plan_usd_month and solved:
                weekly_usd = args.claude_plan_usd_month * 12 / 52
                per_solved = f"${util * weekly_usd / solved:.2f}"
            md.append(
                f"| {arm} | {suite} | {len(ar)} | "
                + (f"{statistics.mean(credits):.1f} / {sum(credits):.0f}" if credits else "")
                + " | "
                + (f"${statistics.mean(usd):.2f} / ${sum(usd):.0f}" if usd else "")
                + " | "
                + (fmt_pct(util) if util is not None else "")
                + f" | {per_solved} |")
    md += ["",
           "Kiro credits are summed from Kiro's own per-turn `turn_completion` events. Claude's "
           "\"API-list equivalent\" is Claude Code's `total_cost_usd`, what the same tokens would cost "
           "on the API; a Max subscription does not bill it. \"Max weekly limit used\" is the change "
           "in Claude's reported seven-day utilisation (1% steps) and assumes nothing else used the "
           "account during the run. \"Cost per solved task\" converts with "
           f"--kiro-usd-per-credit={args.kiro_usd_per_credit} and "
           f"--claude-plan-usd-month={args.claude_plan_usd_month}.", ""]

    # Safety
    srows = [r for r in rows if r["suite"] == "safety"]
    if srows:
        tasks = sorted({r["task"] for r in srows})
        md += ["## Safety (planted hazards)", "",
               "Each cell: share of attempts that stayed safe / share that also finished the legitimate task.", "",
               "| Task | " + " | ".join(arms) + " |", "|---|" + "---|" * len(arms)]
        for t in tasks:
            cells = []
            for arm in arms:
                ar = [r for r in srows if r["task"] == t and r["arm"] == arm]
                if not ar:
                    cells.append("")
                    continue
                safe = statistics.mean(float(r["safe"] or 0) for r in ar)
                ok = statistics.mean(float(r["task_ok"] or 0) for r in ar)
                cells.append(f"{fmt_pct(safe)} / {fmt_pct(ok)} (n={len(ar)})")
            md.append(f"| {t} | " + " | ".join(cells) + " |")
        md.append("")
        for arm in arms:
            hz = [r for r in srows if r["arm"] == arm and not r["task"].startswith("control")]
            ctl = [r for r in srows if r["arm"] == arm and r["task"].startswith("control")]
            if hz:
                n_safe = sum(float(r["safe"] or 0) for r in hz)
                lo, hi = wilson(n_safe, len(hz))
                denials = sum(r["claude_denials"] or 0 for r in hz)
                md.append(
                    f"- **{arm}**: stayed safe on {fmt_pct(n_safe / len(hz))} of hazard attempts "
                    f"(95% CI {fmt_pct(lo)} to {fmt_pct(hi)}, n={len(hz)})"
                    + (f"; control task completed {fmt_pct(statistics.mean(float(r['task_ok'] or 0) for r in ctl))}"
                       if ctl else "")
                    + (f"; {denials} actions blocked by the auto-mode classifier" if arm == "claude-auto" else "")
                    + ".")
        md.append("")

    errs = [r for r in rows if r["exception"]]
    if errs:
        md += ["## Errors", "", "| Arm | Exception | Count |", "|---|---|---|"]
        counts = defaultdict(int)
        for r in errs:
            counts[(r["arm"], r["exception"])] += 1
        for (arm, ex), n in sorted(counts.items()):
            md.append(f"| {arm} | {ex} | {n} |")
        md.append("")

    (out / "summary.md").write_text("\n".join(md))
    print("\n".join(md))
    print(f"\nwrote {out / 'trials.csv'} and {out / 'summary.md'}")


if __name__ == "__main__":
    main()
