"""Harbor's built-in Claude Code adapter, plus the final-diff capture.

The only change from `-a claude-code` is the `capture_final_diff` call that
the Kiro adapter makes too, so diff-based metrics exist for both harnesses.
"""

from typing import override

from harbor.agents.installed.claude_code import ClaudeCode
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from harness_bench.final_diff import capture_final_diff


class ClaudeCodeBench(ClaudeCode):
    @override
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        try:
            await super().run(instruction, environment, context)
        finally:
            await capture_final_diff(self, environment)
