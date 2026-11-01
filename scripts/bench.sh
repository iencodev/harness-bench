#!/usr/bin/env bash
# One entry point for every run, so each arm gets the same runner flags.
#
#   scripts/bench.sh download                # fetch SWE-rebench 07-2026 + Terminal-Bench 2.1 specs
#   scripts/bench.sh validate                # oracle + hazard-oracle on the safety suite (no model calls)
#   scripts/bench.sh smoke <arm>             # 1 safety task + 1 SWE task, 1 attempt
#   scripts/bench.sh run <suite> <arm>       # suite: safety | swe | tb
#   scripts/bench.sh report                  # aggregate jobs/ into reports/
#
# Arms:
#   claude-auto     Claude Code, --permission-mode auto (classifier-gated; the configuration we'd use)
#   claude-bypass   Claude Code, bypassPermissions (no classifier; isolates what the classifier adds)
#   kiro            Kiro CLI, --trust-all-tools (its only unattended mode)
#   kiro-sandbox    Kiro CLI, --trust-all-tools --sandbox (Kiro's own containment, if it starts in a container)
#
# Env (from .env, see .env.example): CLAUDE_CODE_OAUTH_TOKEN, KIRO_API_KEY,
# optional EFFORT, K (attempts), N (concurrency), CLAUDE_VERSION.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] && set -a && . ./.env && set +a

CLAUDE_MODEL="anthropic/claude-opus-5-5"
KIRO_MODEL="kiro/claude-opus-5.5"
# Pinned so every Claude trial runs the same harness build. Kiro's installer
# always fetches its latest release; its version is recorded per trial instead.
CLAUDE_VERSION="${CLAUDE_VERSION:-2.1.316}"
N="${N:-2}"

harbor() { uv run harbor "$@"; }

arm_args() {
  local arm="$1"
  local effort=()
  [ -n "${EFFORT:-}" ] && effort=(--ak "reasoning_effort=${EFFORT}")
  case "$arm" in
    claude-auto|claude-bypass)
      : "${CLAUDE_CODE_OAUTH_TOKEN:?set CLAUDE_CODE_OAUTH_TOKEN in .env (run: claude setup-token)}"
      local mode=auto; [ "$arm" = claude-bypass ] && mode=bypassPermissions
      # CLAUDE_FORCE_OAUTH makes Harbor's adapter bill the Max subscription,
      # not an API key, so usage shows up against the plan's limits.
      ARGS=(-a claude-code -m "$CLAUDE_MODEL" --ak "version=${CLAUDE_VERSION}"
            --ak "permission_mode=${mode}" --ae CLAUDE_FORCE_OAUTH=1 "${effort[@]}") ;;
    kiro|kiro-sandbox)
      : "${KIRO_API_KEY:?set KIRO_API_KEY in .env (Kiro headless needs an API key)}"
      local trust=all; [ "$arm" = kiro-sandbox ] && trust=sandbox
      ARGS=(-a harness_bench.kiro_cli:KiroCli -m "$KIRO_MODEL" --ak "trust_mode=${trust}" "${effort[@]}") ;;
    *) echo "unknown arm: $arm" >&2; exit 2 ;;
  esac
}

suite_args() {
  local suite="$1"
  case "$suite" in
    safety) SUITE=(-p tasks/safety); K="${K:-5}" ;;
    swe|tb)
      local dir=datasets/swe-rebench-07-2026 sel=selections/swe.txt
      [ "$suite" = tb ] && dir=datasets/terminal-bench-2-1 && sel=selections/tb.txt
      [ -f "$sel" ] || { echo "$sel missing: run scripts/sample_tasks.py" >&2; exit 2; }
      SUITE=(-p "$dir")
      while read -r t; do [ -n "$t" ] && SUITE+=(-i "*${t}"); done < "$sel"
      K="${K:-3}" ;;
    *) echo "unknown suite: $suite" >&2; exit 2 ;;
  esac
}

stamp() { date -u +%Y%m%dT%H%M%SZ; }

case "${1:-}" in
  download)
    harbor dataset download ibragim-badertdinov/swe-rebench-07-2026 -o datasets
    harbor dataset download terminal-bench/terminal-bench-2-1 -o datasets ;;
  validate)
    harbor run -p tasks/safety -a oracle -n "$N" -o jobs --job-name "validate__oracle__$(stamp)" -y
    harbor run -p tasks/safety -a harness_bench.hazard_oracle:HazardOracle -n "$N" -o jobs \
      --job-name "validate__hazard-oracle__$(stamp)" -y ;;
  smoke)
    arm_args "${2:?arm}"
    first_swe=$(head -1 selections/swe.txt)
    harbor run -p tasks/safety -i "*control-requested-rewrite" "${ARGS[@]}" -k 1 -n 1 \
      -o jobs --job-name "smoke__${2}__safety__$(stamp)" -y
    harbor run -p datasets/swe-rebench-07-2026 -i "*${first_swe}" "${ARGS[@]}" -k 1 -n 1 \
      -o jobs --job-name "smoke__${2}__swe__$(stamp)" -y ;;
  run)
    suite_args "${2:?suite}"; arm_args "${3:?arm}"
    # Retry once, and only for infrastructure faults, identically for every arm.
    # A harness that crashes or times out on its own keeps the failure.
    # ApiUsageLimitError (plan quota exhausted) is deliberately not retried: the
    # report surfaces it, and the run should be paused until the quota resets.
    RETRY=()
    for e in EnvironmentStartTimeoutError AgentSetupTimeoutError NetworkConnectionError \
             ApiOverloadedError ApiInternalServerError ApiConnectionClosedError ApiRateLimitError; do
      RETRY+=(--retry-include "$e")
    done
    harbor run "${SUITE[@]}" "${ARGS[@]}" -k "$K" -n "$N" --max-retries 1 "${RETRY[@]}" \
      -o jobs --job-name "${2}__${3}__$(stamp)" -y ;;
  report)
    uv run python scripts/report.py "${@:2}" ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
