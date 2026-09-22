from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.messaging import HandoffEnvelope, HandoffParty, HandoffService, HandoffType
from app.orchestration.models import ALLOWED_TRANSITIONS, Task, TaskState
from app.storage import ArtifactReference, ArtifactStore, ArtifactType
from app.verification import (
    CompletionDecision,
    CompletionGuard,
    ReviewIssue,
    ReviewReport,
    ReviewVerdict,
    VerificationPlan,
    VerificationReport,
    Verifier,
)
from app.workspace import CommandResult, WorktreeHandle, WorktreeManager


class OrchestrationError(RuntimeError):
    """Raised when a workflow stage cannot produce valid evidence."""


class PlanDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    planner: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=1000)
    content: dict[str, JsonValue]


class ImplementationOutcome(BaseModel):
    """Implementer output; it is evidence, never a success decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    implementer: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=1000)
    command_results: tuple[CommandResult, ...] = ()


class ReviewDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    reviewer: str = Field(min_length=1, max_length=100)
    verdict: ReviewVerdict
    issues: tuple[ReviewIssue, ...] = ()
    summary: str = Field(min_length=1, max_length=4000)


class PlannerRunner(Protocol):
    async def plan(self, task: Task) -> PlanDraft: ...


class ImplementerRunner(Protocol):
    async def implement(
        self, task: Task, worktree: WorktreeHandle, plan: ArtifactReference
    ) -> ImplementationOutcome: ...


class ReviewerRunner(Protocol):
    async def review(
        self, task: Task, plan: ArtifactReference, verification: VerificationReport
    ) -> ReviewDraft: ...


class OrchestrationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task: Task
    worktree: WorktreeHandle
    plan: ArtifactReference
    implementation: ImplementationOutcome
    verification: VerificationReport
    review: ReviewReport
    completion: CompletionDecision
    handoff_ids: tuple[UUID, ...]


class Orchestrator:
    """Drive one Planner -> Implementer -> Verifier -> Reviewer attempt."""

    def __init__(
        self,
        *,
        planner: PlannerRunner,
        implementer: ImplementerRunner,
        reviewer: ReviewerRunner,
        worktrees: WorktreeManager,
        verifier: Verifier,
        completion_guard: CompletionGuard,
        handoffs: HandoffService,
        artifacts: ArtifactStore,
    ) -> None:
        databases = (
            handoffs.artifacts.database.path,
            verifier.artifacts.database.path,
            completion_guard.artifacts.database.path,
        )
        if any(path != artifacts.database.path for path in databases):
            raise ValueError("orchestrator components must share one artifact database")
        self.planner = planner
        self.implementer = implementer
        self.reviewer = reviewer
        self.worktrees = worktrees
        self.verifier = verifier
        self.completion_guard = completion_guard
        self.handoffs = handoffs
        self.artifacts = artifacts

    async def run_once(
        self,
        task: Task,
        *,
        verification_plan: VerificationPlan,
        base_revision: str = "HEAD",
    ) -> OrchestrationResult:
        if task.state is not TaskState.CREATED:
            raise OrchestrationError("run_once requires a task in the created state")

        correlation_id = uuid4()
        handoff_ids: list[UUID] = []
        try:
            task.transition_to(TaskState.PLANNING)
            draft = await self.planner.plan(task)
            plan = self._persist_plan(task, draft)
            cause = self._handoff(
                task,
                correlation_id=correlation_id,
                causation_id=None,
                sender=HandoffParty.PLANNER,
                recipient=HandoffParty.IMPLEMENTER,
                type=HandoffType.PLAN_READY,
                payload={"planner": draft.planner, "summary": draft.summary},
                artifacts=(plan,),
            )
            handoff_ids.append(cause)

            task.transition_to(TaskState.IMPLEMENTING)
            worktree = await self.worktrees.create(
                task_id=task.id,
                repository=Path(task.repository_path),
                base_revision=base_revision,
            )
            implementation = await self.implementer.implement(task, worktree, plan)
            self._validate_command_results(task, implementation.command_results)
            cause = self._handoff(
                task,
                correlation_id=correlation_id,
                causation_id=cause,
                sender=HandoffParty.IMPLEMENTER,
                recipient=HandoffParty.VERIFIER,
                type=HandoffType.IMPLEMENTATION_READY,
                payload={
                    "implementer": implementation.implementer,
                    "summary": implementation.summary,
                    "command_count": len(implementation.command_results),
                },
                artifacts=tuple(
                    result.audit_artifact for result in implementation.command_results
                ),
            )
            handoff_ids.append(cause)

            task.transition_to(TaskState.VERIFYING)
            verification = await self.verifier.verify(
                worktree,
                trace_id=task.trace_id,
                plan=verification_plan,
                prior_command_results=implementation.command_results,
            )
            evidence = [verification.artifact]
            if verification.change_set.diff_artifact is not None:
                evidence.append(verification.change_set.diff_artifact)
            cause = self._handoff(
                task,
                correlation_id=correlation_id,
                causation_id=cause,
                sender=HandoffParty.VERIFIER,
                recipient=HandoffParty.REVIEWER,
                type=HandoffType.VERIFICATION_READY,
                payload={"passed": verification.passed},
                artifacts=tuple(evidence),
            )
            handoff_ids.append(cause)

            task.transition_to(TaskState.REVIEWING)
            review_draft = await self.reviewer.review(task, plan, verification)
            review = self._persist_review(task, review_draft)
            cause = self._handoff(
                task,
                correlation_id=correlation_id,
                causation_id=cause,
                sender=HandoffParty.REVIEWER,
                recipient=HandoffParty.COMPLETION_GUARD,
                type=(
                    HandoffType.REVIEW_APPROVED
                    if review.verdict is ReviewVerdict.APPROVED
                    else HandoffType.REVIEW_REJECTED
                ),
                payload={
                    "reviewer": review.reviewer,
                    "verdict": review.verdict.value,
                    "issue_count": len(review.issues),
                },
                artifacts=(review.artifact,),
            )
            handoff_ids.append(cause)

            completion = self.completion_guard.evaluate(verification, review)
            task.transition_to(TaskState.COMPLETED if completion.passed else TaskState.REWORK)
            return OrchestrationResult(
                task=task,
                worktree=worktree,
                plan=plan,
                implementation=implementation,
                verification=verification,
                review=review,
                completion=completion,
                handoff_ids=tuple(handoff_ids),
            )
        except Exception:
            if TaskState.FAILED in ALLOWED_TRANSITIONS[task.state]:
                task.transition_to(TaskState.FAILED)
            raise

    def _persist_plan(self, task: Task, draft: PlanDraft) -> ArtifactReference:
        metadata = self.artifacts.put_json(
            draft.content,
            task_id=task.id,
            trace_id=task.trace_id,
            type=ArtifactType.PLAN,
            created_by=draft.planner,
            filename="implementation-plan.json",
        )
        return ArtifactReference.from_metadata(metadata, summary=draft.summary)

    def _persist_review(self, task: Task, draft: ReviewDraft) -> ReviewReport:
        content = {
            "task_id": str(task.id),
            "trace_id": str(task.trace_id),
            "reviewer": draft.reviewer,
            "verdict": draft.verdict.value,
            "issues": [issue.model_dump(mode="json") for issue in draft.issues],
            "summary": draft.summary,
        }
        metadata = self.artifacts.put_json(
            content,
            task_id=task.id,
            trace_id=task.trace_id,
            type=ArtifactType.REVIEW_REPORT,
            created_by=draft.reviewer,
            filename="review-report.json",
            metadata={"verdict": draft.verdict.value},
        )
        return ReviewReport(
            task_id=task.id,
            trace_id=task.trace_id,
            reviewer=draft.reviewer,
            verdict=draft.verdict,
            issues=draft.issues,
            summary=draft.summary,
            artifact=ArtifactReference.from_metadata(metadata, summary=draft.summary),
        )

    def _handoff(
        self,
        task: Task,
        *,
        correlation_id: UUID,
        causation_id: UUID | None,
        sender: HandoffParty,
        recipient: HandoffParty,
        type: HandoffType,
        payload: Mapping[str, JsonValue],
        artifacts: Sequence[ArtifactReference],
    ) -> UUID:
        message = self.handoffs.send(
            HandoffEnvelope(
                task_id=task.id,
                trace_id=task.trace_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
                idempotency_key=f"{task.id}:{task.rework_rounds}:{type.value}",
                sender=sender,
                recipient=recipient,
                type=type,
                payload=dict(payload),
                artifacts=tuple(artifacts),
            )
        )
        batch = self.handoffs.receive(recipient, task_id=task.id, limit=1)
        if batch.rejected or not batch.accepted:
            raise OrchestrationError(f"handoff delivery failed: {type.value}")
        delivered = batch.accepted[0]
        if delivered.envelope.message_id != message.envelope.message_id:
            raise OrchestrationError("mailbox delivered an unexpected task message")
        self.handoffs.mailbox.acknowledge(delivered.envelope.message_id, recipient=recipient)
        return delivered.envelope.message_id

    @staticmethod
    def _validate_command_results(task: Task, results: Sequence[CommandResult]) -> None:
        if any(result.task_id != task.id for result in results):
            raise OrchestrationError("implementer command result belongs to another task")
        if any(result.trace_id != task.trace_id for result in results):
            raise OrchestrationError("implementer command result belongs to another trace")
