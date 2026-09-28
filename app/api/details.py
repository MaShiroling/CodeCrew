"""Read-only, task-scoped views for the future task detail interface."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.orchestration.models import TaskState
from app.storage import ArtifactMetadata, ArtifactReference
from app.storage.continuation_cancellations import ContinuationCancellationReceipt
from app.storage.continuation_workflows import ContinuationWorkflowOutcome
from app.storage.continuations import ContinuationStatus
from app.team import MemberRole, MessageType, PlanRevision, TeamRoom
from app.team.budgets import (
    ConversationBudgetPolicy,
    ConversationBudgetUsage,
    ConversationBudgetViolation,
)


class RoomMessageView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(gt=0)
    message_id: UUID
    sender_id: UUID
    sender_name: str
    sender_role: MemberRole
    recipient_ids: tuple[UUID, ...]
    type: MessageType
    content: str
    artifacts: tuple[ArtifactReference, ...]
    reply_to: UUID | None
    pending_for_human: bool
    pending_for_continuation: bool
    correlation_id: UUID
    created_at: AwareDatetime


class RoomMessagePage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[RoomMessageView, ...]
    limit: int = Field(ge=1, le=100)
    after_sequence: int = Field(ge=0)
    next_after_sequence: int | None = Field(default=None, ge=1)


class HumanMessageReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message: RoomMessageView
    task_revision: int = Field(ge=1)
    agent_dispatched: Literal[False] = False


class ContinueTaskPreflight(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: Literal["continuation-preflight"] = "continuation-preflight"
    task_id: UUID
    trace_id: UUID
    task_revision: int = Field(ge=1)
    runtime_revision: int = Field(ge=1)
    message_id: UUID
    correlation_id: UUID
    target_role: MemberRole
    target_member_id: UUID
    rework_rounds: int = Field(ge=0)
    max_rework_rounds: int = Field(ge=0)
    budget_usage: ConversationBudgetUsage
    checks_passed: Literal[True] = True
    execution_ready: Literal[False] = False
    agent_dispatched: Literal[False] = False


class TaskControlView(BaseModel):
    """Read-only UI snapshot; every command still performs its own checks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    task_state: TaskState
    task_revision: int = Field(ge=1)
    runtime_revision: int = Field(ge=1)
    rework_rounds: int = Field(ge=0)
    max_rework_rounds: int = Field(ge=0)
    budget_policy: ConversationBudgetPolicy
    budget_usage: ConversationBudgetUsage
    budget_violation: ConversationBudgetViolation | None
    latest_continuation: ContinuationStatus | None = None
    latest_workflow_outcome: ContinuationWorkflowOutcome | None = None
    latest_cancellation: ContinuationCancellationReceipt | None = None


class TaskRoomView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    room: TeamRoom


class PlanPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[PlanRevision, ...]


class ArtifactDetail(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metadata: ArtifactMetadata
    preview: str | None = None
    preview_unavailable_reason: str | None = None
