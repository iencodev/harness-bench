"""Record what an agent actually changed, the same way for every harness.

Runs right after the agent finishes and before the verifier: writes the git
diff of the task repo (tracked changes plus new files) to
/logs/agent/final.diff. Edits done through shell commands (sed -i, heredocs)
appear here even though they are not "edit" tool calls, so diff-based metrics
don't depend on how a harness chooses to write files.
"""

from harbor.environments.base import BaseEnvironment

# Same repo search order as the SWE-rebench reference solutions.
_CAPTURE = r"""
d="$PWD"
if [ ! -d "$d/.git" ]; then
  for c in /testbed /app /workspace /repo; do [ -d "$c/.git" ] && d="$c" && break; done
fi
[ -d "$d/.git" ] || exit 0
cd "$d"
{
  git -c core.quotepath=off diff --no-color --no-ext-diff HEAD
  # New files, shown as diffs against /dev/null, without touching the index.
  git ls-files --others --exclude-standard -z | xargs -0 -r -I{} git diff --no-color --no-index /dev/null {} 2>/dev/null
} > "$OUT" 2>/dev/null || true
"""


async def capture_final_diff(agent, environment: BaseEnvironment) -> None:
    out = (agent.environment_logs_dir / "final.diff").as_posix()
    try:
        await environment.exec(command=_CAPTURE, env={"OUT": out}, timeout_sec=120)
    except Exception:  # never let bookkeeping fail a trial
        agent.logger.warning("final diff capture failed", exc_info=True)
