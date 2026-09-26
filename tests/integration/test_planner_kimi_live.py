"""Opt-in four-turn real Codex Planner and Kimi Implementer chat handoff."""

import json
import os
import platform
import shutil

import pytest

from app.agents import CodexCliAdapter, KimiCodeAdapter
from scripts.planner_kimi_smoke import handoff_fixture, run_handoff

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("CODECREW_RUN_PLANNER_KIMI_LIVE") != "1",
    reason="enable CODECREW_RUN_PLANNER_KIMI_LIVE=1 to spend four real Agent turns",
)
async def test_live_planner_clarification_and_kimi_implementation(tmp_path):
    if platform.system() != "Darwin":
        pytest.fail("live handoff requires macOS Seatbelt")
    for executable in ("codex", "kimi"):
        if shutil.which(executable) is None:
            pytest.fail(f"{executable} CLI is not on PATH")
    if not os.environ.get("KIMI_MODEL_API_KEY", "").strip():
        pytest.fail("set KIMI_MODEL_API_KEY in this terminal; never put it in the command")

    def implementer(worktrees, runtime, policy):
        return KimiCodeAdapter(
            worktree_root=worktrees, runtime_root=runtime, policy=policy, max_steps_per_turn=12
        )

    async with handoff_fixture(tmp_path, CodexCliAdapter(), implementer) as fixture:
        report = await run_handoff(fixture)
        assert len(fixture.turns) == 4
        print(
            json.dumps(
                {
                    "trace_id": str(fixture.task.trace_id),
                    "database": str(fixture.store.database.path),
                    "artifact_root": str(fixture.store.root),
                    "verification_artifact_id": str(report.artifact.artifact_id),
                    "task_success": False,
                }
            )
        )
