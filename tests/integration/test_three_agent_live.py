"""Explicit opt-in: two Codex, two Kimi and one independent DeepSeek turn."""

import json
import os
import platform
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from app.agents import CodexCliAdapter, DeepSeekClaudeReviewerAdapter, KimiCodeAdapter
from app.orchestration.models import TaskState
from scripts.planner_kimi_smoke import handoff_fixture
from scripts.three_agent_smoke import run_three_agent

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("CODECREW_RUN_THREE_AGENT_LIVE") != "1",
    reason="enable CODECREW_RUN_THREE_AGENT_LIVE=1 to spend up to five real Agent turns",
)
async def test_live_three_agent_success_path(tmp_path):
    if platform.system() != "Darwin":
        pytest.fail("three Agent live smoke requires macOS Seatbelt")
    for name in ("codex", "kimi", "claude"):
        if shutil.which(name) is None:
            pytest.fail(f"{name} CLI is not on PATH")
    for key in ("KIMI_MODEL_API_KEY", "DEEPSEEK_API_KEY"):
        if not os.environ.get(key, "").strip():
            pytest.fail(f"set {key} in this terminal; never put it in the command")

    def implementer(worktrees, runtime, policy):
        return KimiCodeAdapter(
            worktree_root=worktrees, runtime_root=runtime, policy=policy, max_steps_per_turn=12
        )

    with TemporaryDirectory(prefix="reviewer-home-", dir=tmp_path) as home:
        reviewer = DeepSeekClaudeReviewerAdapter(env_source={**os.environ, "HOME": str(Path(home))})
        async with handoff_fixture(
            tmp_path, CodexCliAdapter(), implementer, reviewer=reviewer
        ) as fixture:
            result = await run_three_agent(fixture)
            assert fixture.task.state is TaskState.COMPLETED
            assert result.runtime.latest_completion and result.runtime.latest_completion.passed
            assert len(result.workflow.agent_turns) == 5
            print(
                json.dumps(
                    {
                        "trace_id": str(fixture.task.trace_id),
                        "database": str(fixture.store.database.path),
                        "artifact_root": str(fixture.store.root),
                        "report_artifact_id": str(result.report.artifact_id),
                        "completion_artifact_id": str(
                            result.runtime.latest_completion.artifact.artifact_id
                        ),
                        "task_success": True,
                    }
                )
            )
