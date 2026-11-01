#!/usr/bin/env bash
# One entry point for every run, so each arm gets the same runner flags.
#
#   scripts/bench.sh download                # fetch SWE-rebench 07-2026 + Terminal-Bench 2.1 specs
#   scripts/bench.sh validate                # oracle + hazard-oracle on the safety suite (no model calls)
#   scripts/bench.sh smoke <arm>             # 1 safety task + 1 SWE task, 1 attempt
#   scripts/bench.sh run <suite> <arm>       # suite: safety | swe | tb
#   scripts/bench.sh judge <suite>           # blind the transcripts, grade decision quality (rubrics/)
#   scripts/bench.sh report [args]           # aggregate jobs/ into reports/
#   scripts/bench.sh models                  # list supported MODEL values
#
# Arms:
#   claude-auto     Claude Code, --permission-mode auto (classifier-gated; the configuration we'd use)
#   claude-bypass   Claude Code, bypassPermissions (no classifier; isolates what the classifier adds)
#   kiro            Kiro CLI, --trust-all-tools (its only unattended mode)
#   kiro-sandbox    Kiro CLI, --trust-all-tools --sandbox (Kiro's own containment, if it starts in a container)
#
# Env (from .env, see .env.example): CLAUDE_CODE_OAUTH_TOKEN, KIRO_API_KEY,
# MODEL (default opus-5.5), EFFORT, K (attempts), N (concurrency), CLAUDE_VERSION,
# JUDGE_AGENT / JUDGE_MODEL for `judge`.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] && set -a && . ./.env && set +a

# Same model on both sides. Each line: alias, Claude Code model id, Kiro model id
# ("-" = Kiro doesn't offer it; checked with `kiro-cli chat --list-models`, Nov 2026).
MODELS="
opus-5.5    anthropic/claude-opus-5-5    claude-opus-5.5
sonnet-5.5  anthropic/claude-sonnet-5-5  claude-sonnet-5.5
haiku-5.5   anthropic/claude-haiku-5-5   claude-haiku-5.5
fable-5.1   anthropic/claude-fable-5-1   -
"
MODEL="${MODEL:-opus-5.5}"
read -r _ CLAUDE_MODEL KIRO_MODEL <<<"$(echo "$MODELS" | awk -v m="$MODEL" '$1 == m')" || true
[ -n "${CLAUDE_MODEL:-}" ] || { echo "unknown MODEL=$MODEL; see: scripts/bench.sh models" >&2; exit 2; }

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
      # Harbor's Claude Code adapter plus final-diff capture (harness_bench/claude_code.py).
      # CLAUDE_FORCE_OAUTH bills the Max subscription, not an API key.
      ARGS=(-a harness_bench.claude_code:ClaudeCodeBench -m "$CLAUDE_MODEL"
            --ak "version=${CLAUDE_VERSION}" --ak "permission_mode=${mode}"
            --ae CLAUDE_FORCE_OAUTH=1 "${effort[@]}") ;;
    kiro|kiro-sandbox)
      [ "$KIRO_MODEL" != "-" ] || { echo "Kiro does not offer MODEL=$MODEL; run it on Claude arms only" >&2; exit 2; }
      : "${KIRO_API_KEY:?set KIRO_API_KEY in .env (Kiro headless needs an API key)}"
      local trust=all; [ "$arm" = kiro-sandbox ] && trust=sandbox
      ARGS=(-a harness_bench.kiro_cli:KiroCli -m "kiro/${KIRO_MODEL}" --ak "trust_mode=${trust}" "${effort[@]}") ;;
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
  models)
    echo "MODEL       Claude Code                  Kiro"; echo "$MODELS" | sed '/^$/d' ;;
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
      -o jobs --job-name "smoke__${2}__${MODEL}__safety__$(stamp)" -y
    harbor run -p datasets/swe-rebench-07-2026 -i "*${first_swe}" "${ARGS[@]}" -k 1 -n 1 \
      -o jobs --job-name "smoke__${2}__${MODEL}__swe__$(stamp)" -y ;;
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
      -o jobs --job-name "${2}__${3}__${MODEL}__$(stamp)" -y ;;
  judge)
    suite="${2:?suite}"
    : "${JUDGE_AGENT:?set JUDGE_AGENT (e.g. codex or claude-code); prefer a non-Anthropic judge, see README}"
    : "${JUDGE_MODEL:?set JUDGE_MODEL for the judge agent}"
    uv run python scripts/blind.py --suite "$suite" ${JUDGE_SAMPLE:+--sample "$JUDGE_SAMPLE"}
    judge_tag="$(echo "$JUDGE_MODEL" | tr '/:.' '___')"
    judge_job="analysis__${suite}__${judge_tag}__$(stamp)"
    # A Claude Code judge on the Max plan needs the same OAuth switch as the arms.
    judge_env=(); [ "$JUDGE_AGENT" = claude-code ] && judge_env=(--ae CLAUDE_FORCE_OAUTH=1)
    harbor analyze "jobs-judge/judge__${suite}" -r rubrics/decision-quality.toml \
      -p rubrics/judge-prompt.txt -a "$JUDGE_AGENT" -m "$JUDGE_MODEL" "${judge_env[@]}" \
      -n "$N" -o jobs-judge --job-name "$judge_job"
    # Kept per judge model, so several judges can be compared for agreement.
    mkdir -p reports/judge
    cp "jobs-judge/${judge_job}/analysis.json" "reports/judge/${judge_job}.json"
    echo "judge results -> reports/judge/${judge_job}.json" ;;
  report)
    uv run python scripts/report.py "${@:2}" ;;
  *) sed -n '2,23p' "$0"; exit 2 ;;
esac
