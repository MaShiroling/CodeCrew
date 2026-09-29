"""Task-independent, repository-free team chat data contracts."""

from enum import Enum
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.orchestration.models import utc_now
from app.team.models import (
    MAX_CHAT_CONTENT_CHARS,
    MemberRole,
    MessageDelivery,
    RoomMember,
    RoomStatus,
)

_CHAT_ROLES = frozenset({
    MemberRole.HUMAN,
    MemberRole.PLANNER,
    MemberRole.IMPLEMENTER,
    MemberRole.REVIEWER,
})


class StandaloneChatRoom(BaseModel):
    """One fixed four-member room, with no Task or Git repository association."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    room_id: UUID = Field(default_factory=uuid4)
    trace_id: UUID = Field(default_factory=uuid4)
    title: str = Field(min_length=1, max_length=200)
    status: RoomStatus = RoomStatus.ACTIVE
    members: tuple[RoomMember, ...]
    created_at: AwareDatetime = Field(default_factory=utc_now)
    closed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_room(self) -> "StandaloneChatRoom":
        if {member.role for member in self.members} != _CHAT_ROLES or len(self.members) != 4:
            raise ValueError("standalone chat requires one human and three distinct agents")
        if any(member.room_id != self.room_id for member in self.members):
            raise ValueError("all chat members must belong to the room")
        if len({member.member_id for member in self.members}) != len(self.members):
            raise ValueError("chat member IDs must be unique")
        if len({member.name for member in self.members}) != len(self.members):
            raise ValueError("chat member names must be unique")
        if self.status is RoomStatus.ACTIVE and self.closed_at is not None:
            raise ValueError("active chat cannot have closed_at")
        if self.status is RoomStatus.CLOSED and self.closed_at is None:
            raise ValueError("closed chat requires closed_at")
        return self


class StandaloneChatMessage(BaseModel):
    """A directed discussion message; it cannot carry coding-workflow actions."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    message_id: UUID = Field(default_factory=uuid4)
    room_id: UUID
    trace_id: UUID
    sender_id: UUID
    recipient_ids: tuple[UUID, ...] = Field(min_length=1, max_length=3)
    content: str = Field(min_length=1, max_length=MAX_CHAT_CONTENT_CHARS)
    reply_to: UUID | None = None
    correlation_id: UUID = Field(default_factory=uuid4)
    causation_id: UUID | None = None
    idempotency_key: str = Field(min_length=1, max_length=255)
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_message(self) -> "StandaloneChatMessage":
        if not self.content.strip():
            raise ValueError("chat content cannot be blank")
        if len(set(self.recipient_ids)) != len(self.recipient_ids):
            raise ValueError("chat recipients must be unique")
        if self.sender_id in self.recipient_ids:
            raise ValueError("chat sender cannot address itself")
        if self.reply_to == self.message_id or self.causation_id == self.message_id:
            raise ValueError("chat message cannot reply to or cause itself")
        return self


class StoredStandaloneChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(gt=0)
    message: StandaloneChatMessage
    deliveries: tuple[MessageDelivery, ...]


class ChatTurnStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    BUDGET_EXHAUSTED = "budget_exhausted"


class StandaloneChatTurn(BaseModel):
    """Durable disposition of one message delivery to one Agent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    turn_id: UUID
    room_id: UUID
    message_id: UUID
    recipient_id: UUID
    correlation_id: UUID
    status: ChatTurnStatus
    session_id: UUID | None = None
    error: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
