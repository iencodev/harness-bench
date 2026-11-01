#!/usr/bin/env python3
"""Build an anonymised copy of trials for the decision-quality judge.

For each trial in the selected jobs, writes a judge trial that holds only:
  agent/transcript.json  neutral steps from harness_bench.transcript (no tool names)
  agent/final.diff       the agent's final changes
  verifier/              the verifier's output
  result.json            the reward, with agent, model, job and harness details removed

All arms are mixed into one judge job per suite under random ids, so the judge
can't group runs by harness. The id -> arm mapping goes to reports/blind-map.json,
outside the judge's view, and scripts/report.py joins it back.

    uv run python scripts/blind.py --suite swe [--jobs jobs] [--sample 40 --seed 1]
"""

import argparse
import json
import random
import shutil
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from harbor.models.trial.result import TrialResult  # noqa: E402

from harness_bench.transcript import neutral_transcript, parse_trial  # noqa: E402


def job_meta(name: str) -> dict | None:
    parts = name.split("__")
    if parts[0] in ("validate", "smoke", "judge") or len(parts) < 3:
        return None
    model = parts[2] if len(parts) >= 4 else "opus-5.5"
    return {"suite": parts[0], "arm": parts[1], "model": model}


def scrub_result(r: dict, blind_job: Path, blind_id: str) -> dict:
    task = r.get("config", {}).get("task", {})
    if task.get("path"):
        task["path"] = str((ROOT / task["path"]).resolve()) if not Path(task["path"]).is_absolute() else task["path"]
    r["agent_info"] = {"name": "agent", "version": "0", "model_info": None}
    r["agent_result"] = None
    r["agent_setup"] = None
    r["config"]["agent"] = {"name": "nop"}
    r["config"]["trials_dir"] = str(blind_job)
    r["config"]["trial_name"] = blind_id
    r["trial_name"] = blind_id
    r["trial_uri"] = (blind_job / blind_id).resolve().as_uri()
    if r.get("exception_info"):
        # Messages and tracebacks quote the harness command line.
        r["exception_info"] = {k: (v if k == "exception_type" else None)
                               for k, v in r["exception_info"].items()}
    for step in r.get("step_results") or []:
        if isinstance(step, dict):
            step.pop("agent_result", None)
    return r


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", required=True, choices=["swe", "tb", "safety"])
    ap.add_argument("--jobs", default=str(ROOT / "jobs"))
    ap.add_argument("--out", default=str(ROOT / "jobs-judge"))
    ap.add_argument("--map", default=str(ROOT / "reports" / "blind-map.json"))
    ap.add_argument("--sample", type=int, default=None,
                    help="Judge a random subset of trials (stratified by arm) to cap judge cost.")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    trials = []
    for job in sorted(Path(args.jobs).iterdir()):
        meta = job_meta(job.name)
        if not meta or meta["suite"] != args.suite or not job.is_dir():
            continue
        for t in sorted(job.iterdir()):
            if (t / "result.json").is_file():
                trials.append((job, t, meta))
    if not trials:
        raise SystemExit(f"no {args.suite} trials in {args.jobs}")

    rng = random.Random(args.seed)
    if args.sample:
        by_arm: dict[str, list] = {}
        for item in trials:
            by_arm.setdefault(f"{item[2]['arm']}@{item[2]['model']}", []).append(item)
        per = max(1, args.sample // len(by_arm))
        trials = [x for group in by_arm.values() for x in rng.sample(group, min(per, len(group)))]
    rng.shuffle(trials)

    blind_job = Path(args.out) / f"judge__{args.suite}"
    if blind_job.exists():
        shutil.rmtree(blind_job)
    blind_job.mkdir(parents=True)
    (blind_job / "job.log").write_text("")

    mapping_path = Path(args.map)
    mapping = json.loads(mapping_path.read_text()) if mapping_path.is_file() else {}
    for job, t, meta in trials:
        _, run = parse_trial(t / "agent")
        if run is None:
            continue
        blind_id = uuid.UUID(int=rng.getrandbits(128)).hex[:12]
        d = blind_job / blind_id
        (d / "agent").mkdir(parents=True)
        (d / "trial.log").write_text("")
        (d / "agent" / "transcript.json").write_text(json.dumps(neutral_transcript(run), indent=1))
        if (t / "agent" / "final.diff").is_file():
            shutil.copy(t / "agent" / "final.diff", d / "agent" / "final.diff")
        if (t / "verifier").is_dir():
            shutil.copytree(t / "verifier", d / "verifier")
        r = scrub_result(json.loads((t / "result.json").read_text()), blind_job, blind_id)
        TrialResult.model_validate(r)  # analyze must be able to read it
        (d / "result.json").write_text(json.dumps(r, indent=1))
        mapping[blind_id] = {**meta, "job": job.name, "trial": t.name,
                             "task": r["task_name"].split("/")[-1]}

    mapping_path.parent.mkdir(exist_ok=True)
    mapping_path.write_text(json.dumps(mapping, indent=1))
    n = sum(1 for p in blind_job.iterdir() if p.is_dir())
    print(f"{n} blinded trials -> {blind_job}; mapping -> {mapping_path}")


if __name__ == "__main__":
    main()
