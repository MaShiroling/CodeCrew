from enum import Enum
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.orchestration.models import utc_now
from app.storage import ArtifactReference

MAX_CHAT_CONTENT_CHARS = 16_000
MAX_CHAT_ARTIFACTS = 50
MAX_CHAT_RECIPIENTS = 20


class RoomStatus(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"


class MemberRole(str, Enum):
    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    VERIFIER = "verifier"
    ORCHESTRATOR = "orchestrator"
    HUMAN = "human"


class MemberKind(str, Enum):
    AGENT = "agent"
    SYSTEM = "system"
    HUMAN = "human"


class MessageType(str, Enum):
    ISSUE_POSTED = "issue_posted"
    MESSAGE = "message"
    QUESTION = "question"
    ANSWER = "answer"
    STATUS_UPDATE = "status_update"
    ARTIFACT_SHARED = "artifact_shared"
    PLAN_SHARED = "plan_shared"
    IMPLEMENTATION_READY = "implementation_ready"
    REVIEW_COMMENT = "review_comment"
    REVIEW_REQUEST = "review_request"
    REVIEW_APPROVED = "review_approved"
    REWORK_REQUEST = "rework_request"
    VERIFICATION_READY = "verification_ready"
    COMPLETION_PASSED = "completion_passed"
    COMPLETION_REJECTED = "completion_rejected"
    HUMAN_INPUT_REQUEST = "human_input_request"
    SYSTEM_EVENT = "system_event"


class RecipientKind(str, Enum):
    MEMBER = "member"
    ROLE = "role"
    ROOM = "room"


class RoomMember(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    member_id: UUID = Field(default_factory=uuid4)
    room_id: UUID
    name: str = Field(min_length=1, max_length=100)
    role: MemberRole
    kind: MemberKind
    joined_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_role_kind(self) -> "RoomMember":
        if self.role in {MemberRole.VERIFIER, MemberRole.ORCHESTRATOR}:
            if self.kind is not MemberKind.SYSTEM:
                raise ValueError("verifier and orchestrator members must be system identities")
        elif self.role is MemberRole.HUMAN:
            if self.kind is not MemberKind.HUMAN:
                raise ValueError("human members must use the human identity kind")
        elif self.kind is not MemberKind.AGENT:
            raise ValueError("planner, implementer, and reviewer members must be agents")
        return self


class TeamRoom(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    room_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    trace_id: UUID
    name: str = Field(min_length=1, max_length=200)
    status: RoomStatus = RoomStatus.ACTIVE
    members: tuple[RoomMember, ...] = ()
    created_at: AwareDatetime = Field(default_factory=utc_now)
    closed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_room(self) -> "TeamRoom":
        if any(member.room_id != self.room_id for member in self.members):
            raise ValueError("all members must belong to the room")
        member_ids = [member.member_id for member in self.members]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("room member IDs must be unique")
        if self.status is RoomStatus.ACTIVE and self.closed_at is not None:
            raise ValueError("an active room cannot have closed_at")
        if self.status is RoomStatus.CLOSED and self.closed_at is None:
            raise ValueError("a closed room requires closed_at")
        return self


class MessageRecipient(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: RecipientKind
    member_id: UUID | None = None
    role: MemberRole | None = None

    @model_validator(mode="after")
    def validate_target(self) -> "MessageRecipient":
        if self.kind is RecipientKind.MEMBER:
            if self.member_id is None or self.role is not None:
                raise ValueError("member recipient requires only member_id")
        elif self.kind is RecipientKind.ROLE:
            if self.role is None or self.member_id is not None:
                raise ValueError("role recipient requires only role")
        elif self.member_id is not None or self.role is not None:
            raise ValueError("room recipient cannot specify member_id or role")
        return self


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    message_id: UUID = Field(default_factory=uuid4)
    room_id: UUID
    task_id: UUID
    trace_id: UUID
    sender_id: UUID
    recipients: tuple[MessageRecipient, ...] = Field(
        min_length=1, max_length=MAX_CHAT_RECIPIENTS
    )
    type: MessageType
    content: str = Field(min_length=1, max_length=MAX_CHAT_CONTENT_CHARS)
    artifacts: tuple[ArtifactReference, ...] = Field(
        default=(), max_length=MAX_CHAT_ARTIFACTS
    )
    supersedes_artifact_id: UUID | None = None
    addresses_message_ids: tuple[UUID, ...] = Field(default=(), max_length=50)
    reply_to: UUID | None = None
    correlation_id: UUID = Field(default_factory=uuid4)
    causation_id: UUID | None = None
    idempotency_key: str = Field(min_length=1, max_length=255)
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_message(self) -> "ChatMessage":
        if self.reply_to == self.message_id:
            raise ValueError("a message cannot reply to itself")
        if self.causation_id == self.message_id:
            raise ValueError("a message cannot cause itself")
        if len(set(self.recipients)) != len(self.recipients):
            raise ValueError("message recipients must be unique")
        artifact_ids = [item.artifact_id for item in self.artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("artifact references must be unique")
        if len(self.addresses_message_ids) != len(set(self.addresses_message_ids)):
            raise ValueError("addressed message IDs must be unique")
        if self.type is not MessageType.PLAN_SHARED and (
            self.supersedes_artifact_id is not None or self.addresses_message_ids
        ):
            raise ValueError("plan revision fields are only valid for plan_shared messages")
        return self


class PlanRevision(BaseModel):
    """One immutable, task-scoped version in the implementation plan chain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    room_id: UUID
    task_id: UUID
    trace_id: UUID
    version: int = Field(gt=0)
    artifact_id: UUID
    message_id: UUID
    supersedes_artifact_id: UUID | None = None
    addresses_message_ids: tuple[UUID, ...] = ()
    created_at: AwareDatetime


class MessageDeliveryStatus(str, Enum):
    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"


class MessageDelivery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: UUID
    recipient_id: UUID
    status: MessageDeliveryStatus = MessageDeliveryStatus.PENDING
    acknowledged_at: AwareDatetime | None = None


class StoredChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(gt=0)
    message: ChatMessage
    deliveries: tuple[MessageDelivery, ...] = ()
