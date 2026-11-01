#!/usr/bin/env python3
"""Print how the shared parser reads one trial: every normalised step, then
the process metrics. Use it on the first smoke run of each harness to check
the parser understands that harness's log before trusting the report.

    uv run python scripts/inspect_trial.py jobs/<job>/<trial>
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from harness_bench.transcript import parse_trial, process_metrics  # noqa: E402

trial = Path(sys.argv[1])
harness, run = parse_trial(trial / "agent")
if run is None:
    raise SystemExit("no claude-code.txt or kiro-cli.jsonl under agent/")
print(f"harness log: {harness}")
for i, e in enumerate(run.events, 1):
    detail = e.command or e.path or e.text
    flags = " ".join(f for f, on in (("ERROR", e.error), ("BLOCKED", e.blocked)) if on)
    print(f"{i:3} {e.kind:8} {e.shell or '':6} {flags:8} {(detail or '')[:110]!r}")
diff = trial / "agent" / "final.diff"
print(json.dumps(process_metrics(run, diff.read_text() if diff.is_file() else None, None), indent=1))
