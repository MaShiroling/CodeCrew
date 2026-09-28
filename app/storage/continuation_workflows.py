"""Durable result of an explicitly requested Human-to-guard workflow.

The ordinary continuation receipt still describes only its selected first turn.
This result is recorded as a task-scoped trace in the same transaction that
commits the selected ACK, Runtime CAS, and final Task snapshot.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.orchestration.models import TaskState


class ContinuationWorkflowOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    task_id: UUID
    trace_id: UUID
    final_state: TaskState
    success: bool
    completion_evaluated: bool
    verification_artifact_id: UUID | None = None
    review_artifact_id: UUID | None = None
    completion_artifact_id: UUID | None = None
    agent_session_ids: tuple[UUID, ...] = ()
    rework_rounds: int = Field(ge=0, strict=True)
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_guard(self):
        if self.final_state not in {TaskState.COMPLETED, TaskState.NEEDS_HUMAN}:
            raise ValueError("workflow result must be complete or returned to Human")
        if self.success != (self.final_state is TaskState.COMPLETED):
            raise ValueError("only a completed workflow may report success")
        if self.success and (
            not self.completion_evaluated
            or self.verification_artifact_id is None
            or self.review_artifact_id is None
            or self.completion_artifact_id is None
        ):
            raise ValueError("success requires fresh verification, review and guard evidence")
        if not self.agent_session_ids:
            raise ValueError("workflow result requires a selected Agent turn")
        return self
