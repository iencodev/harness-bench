"""Harbor adapter for the Kiro CLI (kiro-cli), run headless.

Harbor ships adapters for Claude Code, Codex, Cursor, Gemini CLI and others,
but not Kiro, so this file is the one piece of agent glue the benchmark adds.
It mirrors the built-in adapters (install in the task container, run once with
the task instruction, keep the raw event stream) so both harnesses go through
the same runner, container, timeout and verifier.

Usage (see scripts/bench.sh):

    harbor run -a harness_bench.kiro_cli:KiroCli -m kiro/claude-opus-5.5 \
        --ae KIRO_API_KEY=... -p tasks/safety

Auth is a Kiro API key (KIRO_API_KEY), which Kiro's headless mode requires.
Keys are available on Pro and higher plans; on an organisation-managed
subscription an admin has to enable key generation first.
"""

import json
import shlex
import uuid
from typing import Annotated, Any, Literal, override

from pydantic import Field

from harbor.agents.agent_auth_resolution import require_credential_env
from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.options import Cli, InstalledAgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

_OUTPUT_FILENAME = "kiro-cli.jsonl"
_STDERR_FILENAME = "kiro-cli.stderr.txt"
_PATH_SETUP = 'export PATH="$HOME/.local/bin:$PATH"; '


class KiroCliOptions(InstalledAgentOptions):
    # Kiro has no classifier-gated mode (kirodotdev/Kiro#10364), so an unattended
    # run is either "trust every tool" or "trust every tool inside Kiro's command
    # sandbox". "all" is what a CI user of Kiro would run, so it is the default;
    # "sandbox" is the fairest safety comparison when Kiro can start its sandbox in
    # the container (it exits early if it cannot, which the run log will show).
    trust_mode: Literal["all", "sandbox"] = Field(
        default="all", description="all = --trust-all-tools; sandbox = also --sandbox."
    )
    # Pinned, not left to Kiro: in a fresh container kiro-cli 2.37 starts the v2
    # engine, while an installed desktop setup upgrades to v3 (chat.agentEngine).
    # v3 is Kiro's current agent and what its users run, so it is the fair
    # default. The two engines also emit different stream-json events, and the
    # credit accounting below reads v3's.
    engine: Annotated[
        Literal["v2", "v3"], Cli("--agent-engine")
    ] = Field(default="v3", description="Kiro agent engine.")
    # Credits are the unit Kiro bills in. The USD rate depends on the plan, so
    # it stays out of the adapter and is applied by the report (scripts/report.py).


class KiroCli(BaseInstalledAgent):
    capabilities = AgentCapabilities(reasoning_effort=True)
    options_model = KiroCliOptions
    options: KiroCliOptions

    @classmethod
    @override
    def check_model_credentials(cls, connection, *, kwargs, env, explicit_env=None):
        require_credential_env(env, cls.name(), "KIRO_API_KEY")

    @staticmethod
    @override
    def name() -> str:
        return "kiro-cli"

    @override
    def get_version_command(self) -> str | None:
        return f"{_PATH_SETUP}kiro-cli --version"

    @override
    def parse_version(self, stdout: str) -> str:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        return lines[-1].split()[-1] if lines else ""

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        await self.ensure_system_dependencies(
            environment, ("curl", "bash", "unzip", "ca_certificates")
        )
        # Kiro's installer always fetches the current release; the installed
        # version is recorded per trial (get_version_command) instead of pinned.
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                "curl -fsSL https://cli.kiro.dev/install | bash; "
                f"{_PATH_SETUP}"
                "kiro-cli --version; "
                # Harbor containers run as root; the warning is noise in the log.
                "kiro-cli settings app.warnOnElevatedPrivileges false || true"
            ),
        )

    def _model_arg(self) -> str:
        if not self.model_name:
            raise ValueError("Model name is required, e.g. -m kiro/claude-opus-5.5")
        # LiteLLM doesn't know a "kiro" provider, so Harbor leaves the prefix on.
        return self.model_name.removeprefix("kiro/")

    @override
    @with_prompt_template
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        key = self._get_env("KIRO_API_KEY")
        if not key:
            raise ValueError("KIRO_API_KEY is required for headless Kiro runs.")

        flags = [
            "--no-interactive",
            "--trust-all-tools",
            # Same footing as Claude Code, which reads a repo's CLAUDE.md without asking.
            "--trust-workspace",
            "--output-format stream-json",
            f"--model {shlex.quote(self._model_arg())}",
        ]
        if self.options.trust_mode == "sandbox":
            flags.append("--sandbox")
        if self.options.reasoning_effort:
            flags.append(f"--effort {shlex.quote(self.options.reasoning_effort)}")
        extra = self.build_cli_flags()
        if extra:
            flags.append(extra)

        # Pass the instruction through an env var, as the built-in Claude Code
        # adapter does, so quoting in task text can never break the command line.
        var = f"kiro_instruction_{uuid.uuid4().hex}"
        env_var = var.upper()
        logs = self.environment_logs_dir.as_posix()
        await self.exec_as_agent(
            environment,
            command=(
                f"{_PATH_SETUP}"
                f'{var}="${env_var}"; unset {env_var}; '
                f'kiro-cli chat {" ".join(flags)} "${var}" </dev/null '
                f"2>{logs}/{_STDERR_FILENAME} | tee {logs}/{_OUTPUT_FILENAME}"
            ),
            env={"KIRO_API_KEY": key, env_var: instruction, "NO_COLOR": "1"},
        )

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        try:
            summary = summarize_kiro_stream(self.logs_dir / _OUTPUT_FILENAME)
        except ValueError:
            self.logger.exception("Could not summarize Kiro run")
            return
        if summary is None:
            return
        (self.logs_dir / "kiro-summary.json").write_text(json.dumps(summary, indent=2))
        context.metadata = {**(context.metadata or {}), "kiro": summary}


def summarize_kiro_stream(path) -> dict[str, Any] | None:
    """Reduce Kiro's ACP JSON Lines to the numbers the report needs.

    Shared with scripts/report.py so a trial can be re-scored from its raw log.
    Credits come from the per-turn `turn_completion` events, which is what Kiro
    bills; nothing here is estimated.
    """
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    credits = 0.0
    credit_events = 0
    turns = 0
    tool_calls = 0
    status = None
    stop_reason = None
    engine = None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        data = event.get("data") or {}
        if kind == "runStarted":
            engine = data.get("engine")
        elif kind == "runFinished":
            status = data.get("status")
            stop_reason = data.get("stopReason")
        update = data.get("update") or {}
        if update.get("sessionUpdate") == "tool_call":
            tool_calls += 1
        meta = (update.get("_meta") or {}).get("kiro") or {}
        if meta.get("kind") == "turn_completion":
            turns += 1
            for item in meta.get("promptTurnSummaries") or []:
                if item.get("unit") == "credit":
                    credits += float(item.get("usage") or 0)
                    credit_events += 1
    if engine is not None and engine != "v3":
        # Don't report 0 credits for a run whose events we can't read.
        raise ValueError(f"Kiro ran engine {engine!r}; credit parsing supports v3 only")
    return {
        "credits": round(credits, 4),
        "credit_events": credit_events,
        "turns": turns,
        "tool_calls": tool_calls,
        "status": status,
        "stop_reason": stop_reason,
        "engine": engine,
    }
