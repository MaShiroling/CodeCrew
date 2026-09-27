from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.orchestration.models import TaskState
from app.storage.continuation_authorizations import AuthorizeContinuationCommand
from app.storage.continuation_cancellations import CancelContinuationCommand
from app.storage.continuations import QuarantineContinuationCommand
from app.team.models import MemberRole


class CreateTaskRequest(BaseModel):
    """A software change request to be scheduled by the task service."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    issue: str = Field(min_length=1, max_length=16_000)
    repository_path: str = Field(min_length=1, max_length=4_096)


class CancelTaskRequest(BaseModel):
    """Optimistic cancellation based on the revision last seen by the caller."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_revision: int = Field(ge=1)
    reason: str | None = Field(default=None, min_length=1, max_length=1_000)


class PostHumanMessageRequest(BaseModel):
    """Local human intent, never an arbitrary ChatMessage or workflow directive."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_revision: int = Field(ge=1, strict=True)
    idempotency_key: UUID
    content: str = Field(min_length=1, max_length=16_000)
    recipient_role: Literal[
        MemberRole.PLANNER, MemberRole.IMPLEMENTER, MemberRole.REVIEWER, MemberRole.ORCHESTRATOR,
    ] | None = None
    reply_to: UUID | None = None

    @model_validator(mode="after")
    def validate_destination(self) -> "PostHumanMessageRequest":
        if (self.recipient_role is None) == (self.reply_to is None):
            raise ValueError("provide either recipient_role or reply_to, not both")
        return self


class TaskView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    trace_id: UUID
    issue: str
    repository_path: str
    state: TaskState
    rework_rounds: int = Field(ge=0)
    revision: int = Field(ge=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ContinueTaskPreflightRequest(BaseModel):
    """Select an existing human intent, not new content or execution authority."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1, strict=True)
    message_id: UUID
    target_role: Literal[MemberRole.PLANNER, MemberRole.IMPLEMENTER, MemberRole.REVIEWER]


class ContinueTaskRequest(ContinueTaskPreflightRequest):
    """Execute one selected turn; subsequent intents require a recorded grant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    idempotency_key: UUID
    authorization_id: UUID | None = None


class QuarantineContinuationRequest(QuarantineContinuationCommand):
    """Contain a persisted claim without asserting external-process termination."""


class CancelContinuationRequest(CancelContinuationCommand):
    """Request cancellation of a locally owned turn, never release its claim."""


class AuthorizeContinuationRequest(AuthorizeContinuationCommand):
    """Audit a new intent after a committed turn, without dispatching it."""


class TaskPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[TaskView, ...]
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    next_offset: int | None = Field(default=None, ge=0)


class ApiValidationIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    location: tuple[str | int, ...]
    message: str
    type: str


class ApiErrorDetail(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str
    issues: tuple[ApiValidationIssue, ...] | None = None


class ApiErrorResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    error: ApiErrorDetail
