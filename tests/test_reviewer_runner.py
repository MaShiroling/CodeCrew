from pathlib import Path

import pytest

from app.agents import (
    AgentCapability,
    AgentExitReason,
    AgentRegistry,
    AgentRole,
    FakeAgentAdapter,
    FakeAgentScenario,
    PermissionMode,
)
from app.orchestration.models import Task
from app.orchestration.reviewer import AgentReviewerRunner, ReviewerExecutionError
from app.storage import ArtifactReference, ArtifactType
from app.verification import ReviewVerdict
from app.workspace import WorktreeHandle
from tests.test_completion_guard import make_evidence


def make_runner(tmp_path: Path, scenario: FakeAgentScenario):
    tmp_path.mkdir(parents=True, exist_ok=True)
    evidence = make_evidence(tmp_path)
    store, verification, _ = evidence
    plan_metadata = store.put_json(
        {"steps": ["change code"]},
        task_id=verification.task_id,
        trace_id=verification.trace_id,
        type=ArtifactType.PLAN,
        created_by="planner",
        filename="plan.json",
    )
    plan = ArtifactReference.from_metadata(plan_metadata, summary="plan")
    adapter = FakeAgentAdapter(
        scenario,
        name="reviewer",
        capabilities=frozenset({AgentCapability.CODE_REVIEW}),
    )
    registry = AgentRegistry()
    registry.register(
        adapter,
        roles={AgentRole.REVIEWER},
        permission_modes={PermissionMode.READ_ONLY},
    )
    runner = AgentReviewerRunner(registry, store, agent_name="reviewer")
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    worktree = WorktreeHandle(
        task_id=verification.task_id,
        repository_root=tmp_path / "repository",
        worktree_path=worktree_path,
        branch_name="codecrew/test",
        base_revision="a" * 40,
    )
    task = Task(
        id=verification.task_id,
        trace_id=verification.trace_id,
        issue="Implement the requested change",
        repository_path=str(tmp_path / "repository"),
    )
    return runner, adapter, task, worktree, plan, verification


@pytest.mark.asyncio
async def test_runs_independent_read_only_reviewer_and_parses_output(tmp_path: Path) -> None:
    scenario = FakeAgentScenario(
        output={
            "review": {
                "verdict": "approved",
                "summary": "Implementation matches the issue",
                "issues": [],
            }
        }
    )
    runner, adapter, task, worktree, plan, verification = make_runner(tmp_path, scenario)

    review = await runner.review(task, worktree, plan, verification)

    assert review.verdict is ReviewVerdict.APPROVED
    assert review.reviewer == "reviewer"
    assert len(adapter.requests) == 1
    request = adapter.requests[0]
    assert request.role is AgentRole.REVIEWER
    assert request.permission_mode is PermissionMode.READ_ONLY
    assert request.working_directory == worktree.worktree_path
    assert request.metadata["independent_session"] is True
    assert str(plan.artifact_id) == request.metadata["plan_artifact_id"]
    assert str(runner.artifacts.blob_path_for(plan.artifact_id)) in request.prompt
    assert "Read each listed artifact" in request.prompt
    assert "if evidence is missing, failed, or uncertain, reject" in request.prompt


@pytest.mark.asyncio
async def test_accepts_json_text_from_cli_adapter(tmp_path: Path) -> None:
    scenario = FakeAgentScenario(
        output={
            "result": (
                '{"verdict":"rejected","summary":"Bug remains","issues":'
                '[{"priority":"high","summary":"Wrong result","resolved":false}]}'
            )
        }
    )
    runner, _, task, worktree, plan, verification = make_runner(tmp_path, scenario)

    review = await runner.review(task, worktree, plan, verification)

    assert review.verdict is ReviewVerdict.REJECTED
    assert review.issues[0].summary == "Wrong result"


@pytest.mark.asyncio
async def test_rejects_malformed_or_failed_reviewer_output(tmp_path: Path) -> None:
    malformed = FakeAgentScenario(output={"result": "not-json"})
    runner, _, task, worktree, plan, verification = make_runner(tmp_path, malformed)
    with pytest.raises(ReviewerExecutionError, match="not valid JSON"):
        await runner.review(task, worktree, plan, verification)

    failed = FakeAgentScenario(
        reason=AgentExitReason.FAILED,
        exit_code=1,
        error="provider failed",
    )
    runner, _, task, worktree, plan, verification = make_runner(tmp_path / "failed", failed)
    with pytest.raises(ReviewerExecutionError, match="provider failed"):
        await runner.review(task, worktree, plan, verification)
