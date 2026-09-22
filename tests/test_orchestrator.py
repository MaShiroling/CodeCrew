import subprocess
import sys
from pathlib import Path

import pytest

from app.messaging import HandoffService, HandoffType, Mailbox, MailboxMessageStatus
from app.orchestration.models import Task, TaskState
from app.orchestration.orchestrator import (
    ImplementationOutcome,
    OrchestrationError,
    Orchestrator,
    PlanDraft,
    ReviewDraft,
)
from app.storage import ArtifactStore, SQLiteDatabase
from app.verification import (
    CompletionGuard,
    ReviewIssue,
    ReviewIssuePriority,
    ReviewVerdict,
    VerificationCheckKind,
    VerificationCommand,
    VerificationPlan,
    Verifier,
)
from app.workspace import (
    CommandExecutor,
    CommandPolicy,
    CommandRule,
    PermissionGate,
    PermissionPolicy,
    WorkspaceChangeCollector,
    WorktreeManager,
)


def git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


class FakePlanner:
    async def plan(self, task: Task) -> PlanDraft:
        assert task.state is TaskState.PLANNING
        return PlanDraft(
            planner="fake-claude-planner",
            summary="Update the value and verify it",
            content={"steps": ["edit src/app.py", "run verification"]},
        )


class FakeImplementer:
    def __init__(self, *, make_change: bool = True, fail: bool = False) -> None:
        self.make_change = make_change
        self.fail = fail

    async def implement(self, task, worktree, plan) -> ImplementationOutcome:
        assert task.state is TaskState.IMPLEMENTING
        if self.fail:
            raise RuntimeError("implementation crashed")
        if self.make_change:
            (worktree.worktree_path / "src/app.py").write_text("value = 2\n")
        return ImplementationOutcome(
            implementer="fake-codex",
            summary="Changed the requested value",
        )


class FakeReviewer:
    def __init__(self, verdict: ReviewVerdict = ReviewVerdict.APPROVED) -> None:
        self.verdict = verdict

    async def review(self, task, plan, verification) -> ReviewDraft:
        assert task.state is TaskState.REVIEWING
        issues = (
            ()
            if self.verdict is ReviewVerdict.APPROVED
            else (
                ReviewIssue(
                    priority=ReviewIssuePriority.HIGH,
                    summary="Implementation does not satisfy the issue",
                ),
            )
        )
        return ReviewDraft(
            reviewer="fake-claude-reviewer",
            verdict=self.verdict,
            issues=issues,
            summary="Independent review completed",
        )


def make_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    (repository / "src").mkdir()
    (repository / "src/app.py").write_text("value = 1\n")
    (repository / "verify.py").write_text("print('ok')\n")
    git(repository, "add", ".")
    git(
        repository,
        "-c",
        "user.name=CodeCrew Tests",
        "-c",
        "user.email=tests@codecrew.invalid",
        "commit",
        "-m",
        "initial",
    )
    return repository


def make_orchestrator(tmp_path: Path, implementer=None, reviewer=None):
    database = SQLiteDatabase(tmp_path / "codecrew.sqlite3")
    artifacts = ArtifactStore(database, tmp_path / "artifacts")
    handoffs = HandoffService(mailbox=Mailbox(database), artifacts=artifacts)
    handoffs.initialize()
    verifier = Verifier(
        artifacts,
        WorkspaceChangeCollector(artifacts),
        PermissionGate(artifacts, PermissionPolicy(allowed_paths=("src",))),
        CommandExecutor(
            artifacts,
            CommandPolicy(
                rules=(
                    CommandRule(
                        name="verification-script",
                        argv_prefix=(sys.executable, "verify.py"),
                    ),
                )
            ),
        ),
    )
    orchestrator = Orchestrator(
        planner=FakePlanner(),
        implementer=implementer or FakeImplementer(),
        reviewer=reviewer or FakeReviewer(),
        worktrees=WorktreeManager(tmp_path / "worktrees"),
        verifier=verifier,
        completion_guard=CompletionGuard(artifacts),
        handoffs=handoffs,
        artifacts=artifacts,
    )
    return orchestrator, handoffs


def verification_plan() -> VerificationPlan:
    return VerificationPlan(
        commands=tuple(
            VerificationCommand(
                name=name,
                kind=kind,
                argv=(sys.executable, "verify.py"),
            )
            for name, kind in (
                ("static", VerificationCheckKind.STATIC_ANALYSIS),
                ("public", VerificationCheckKind.PUBLIC_TESTS),
                ("hidden", VerificationCheckKind.HIDDEN_TESTS),
            )
        )
    )


@pytest.mark.asyncio
async def test_single_attempt_completes_only_after_guard_passes(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    orchestrator, handoffs = make_orchestrator(tmp_path)
    task = Task(issue="Set value to two", repository_path=str(repository))

    result = await orchestrator.run_once(task, verification_plan=verification_plan())

    assert result.task.state is TaskState.COMPLETED
    assert result.completion.passed
    assert len(result.handoff_ids) == 4
    messages = handoffs.mailbox.list_messages(task_id=task.id)
    assert [message.envelope.type for message in messages] == [
        HandoffType.PLAN_READY,
        HandoffType.IMPLEMENTATION_READY,
        HandoffType.VERIFICATION_READY,
        HandoffType.REVIEW_APPROVED,
    ]
    assert all(message.status is MailboxMessageStatus.ACKNOWLEDGED for message in messages)
    assert len({message.envelope.correlation_id for message in messages}) == 1
    assert messages[0].envelope.causation_id is None
    assert all(
        messages[index].envelope.causation_id == messages[index - 1].envelope.message_id
        for index in range(1, len(messages))
    )
    assert (repository / "src/app.py").read_text() == "value = 1\n"


@pytest.mark.asyncio
async def test_rejected_review_moves_task_to_rework(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    orchestrator, _ = make_orchestrator(
        tmp_path, reviewer=FakeReviewer(ReviewVerdict.REJECTED)
    )
    task = Task(issue="Set value to two", repository_path=str(repository))

    result = await orchestrator.run_once(task, verification_plan=verification_plan())

    assert result.task.state is TaskState.REWORK
    assert not result.completion.passed
    assert result.review.verdict is ReviewVerdict.REJECTED


@pytest.mark.asyncio
async def test_missing_diff_cannot_be_overridden_by_reviewer(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    orchestrator, _ = make_orchestrator(
        tmp_path, implementer=FakeImplementer(make_change=False)
    )
    task = Task(issue="Set value to two", repository_path=str(repository))

    result = await orchestrator.run_once(task, verification_plan=verification_plan())

    assert result.review.verdict is ReviewVerdict.APPROVED
    assert not result.verification.passed
    assert not result.completion.passed
    assert result.task.state is TaskState.REWORK


@pytest.mark.asyncio
async def test_stage_exception_marks_task_failed(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    orchestrator, _ = make_orchestrator(
        tmp_path, implementer=FakeImplementer(fail=True)
    )
    task = Task(issue="Set value to two", repository_path=str(repository))

    with pytest.raises(RuntimeError, match="implementation crashed"):
        await orchestrator.run_once(task, verification_plan=verification_plan())

    assert task.state is TaskState.FAILED


@pytest.mark.asyncio
async def test_run_once_rejects_non_created_task(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    orchestrator, _ = make_orchestrator(tmp_path)
    task = Task(issue="Set value to two", repository_path=str(repository))
    task.transition_to(TaskState.PLANNING)

    with pytest.raises(OrchestrationError, match="created state"):
        await orchestrator.run_once(task, verification_plan=verification_plan())

    assert task.state is TaskState.PLANNING
