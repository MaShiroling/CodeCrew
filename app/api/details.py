"""Read-only, task-scoped views for the future task detail interface."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.storage import ArtifactMetadata, ArtifactReference
from app.team import MemberRole, MessageType, PlanRevision, TeamRoom


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
