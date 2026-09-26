"""Explicit opt-in: two Codex, two Kimi and one independent DeepSeek turn."""

import json
import os
import platform
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from app.agents import CodexCliAdapter, DeepSeekClaudeReviewerAdapter, KimiCodeAdapter
from app.config import Settings
from app.orchestration.models import TaskState
from scripts.planner_kimi_smoke import handoff_fixture
from scripts.smoke_evidence import archive_smoke_evidence
from scripts.three_agent_smoke import run_three_agent

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("CODECREW_RUN_THREE_AGENT_LIVE") != "1",
    reason="enable CODECREW_RUN_THREE_AGENT_LIVE=1 to spend up to five real Agent turns",
)
async def test_live_three_agent_success_path(tmp_path):
    await run_live_three_agent_scenario(tmp_path)


async def run_live_three_agent_scenario(tmp_path, *, scenario="success"):
    expected_turns = {"success": 5, "rework_success": 7, "rework_exhaustion": 9}[scenario]
    # Explicit shell environment only; never load credentials or test settings from .env.
    settings = Settings(_env_file=None)
    planner_timeout = settings.planner_timeout_seconds
    run_options = {}
    if scenario != "success":
        run_options["scenario"] = scenario
    if planner_timeout is not None:
        run_options["planner_timeout_seconds"] = planner_timeout
    if settings.reviewer_structured_output:
        run_options["reviewer_structured_output"] = True
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
            try:
                result = await run_three_agent(fixture, **run_options)
                if scenario == "rework_exhaustion":
                    assert fixture.task.state is TaskState.NEEDS_HUMAN
                    assert fixture.task.rework_rounds == 2
                    assert result.runtime.latest_completion is None
                else:
                    assert fixture.task.state is TaskState.COMPLETED
                    assert result.runtime.latest_completion and result.runtime.latest_completion.passed
                assert len(result.workflow.agent_turns) == expected_turns
            finally:
                turn_failure = sys.exception()
                try:
                    archive = archive_smoke_evidence(
                        fixture.store,
                        fixture.task,
                        root=Path(__file__).resolve().parents[2] / "evals/results" / (
                            "three-agent-live" if scenario == "success" else f"three-agent-{scenario}-live"
                        ),
                    )
                except Exception as archive_error:
                    print(json.dumps({
                        "trace_id": str(fixture.task.trace_id),
                        "archive_error": type(archive_error).__name__,
                        "archive_integrity_verified": False,
                    }))
                    if turn_failure is None:
                        raise
                else:
                    print(json.dumps({
                        "trace_id": str(fixture.task.trace_id),
                        "evidence_archive": str(archive),
                        "task_state": fixture.task.state.value,
                        "archive_integrity_verified": True,
                    }))
            print(
                json.dumps(
                    {
                        "trace_id": str(fixture.task.trace_id),
                        "database": str(fixture.store.database.path),
                        "artifact_root": str(fixture.store.root),
                        "report_artifact_id": str(result.report.artifact_id),
                        "completion_artifact_id": str(
                            result.runtime.latest_completion.artifact.artifact_id
                        ) if result.runtime.latest_completion else None,
                        "task_success": fixture.task.state is TaskState.COMPLETED,
                        "scenario": scenario,
                        "rework_rounds": fixture.task.rework_rounds,
                        "scenario_acceptance_passed": True,
                        "evidence_archive": str(archive),
                    }
                )
            )
