import json
from enum import Enum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from app.orchestration.models import utc_now
from app.storage.models import ArtifactReference

HANDOFF_PROTOCOL_VERSION = "1.0"
MAX_HANDOFF_PAYLOAD_BYTES = 32 * 1024
MAX_HANDOFF_ARTIFACTS = 100


class HandoffType(str, Enum):
    PLAN_READY = "plan_ready"
    IMPLEMENTATION_READY = "implementation_ready"
    VERIFICATION_READY = "verification_ready"
    REVIEW_APPROVED = "review_approved"
    REVIEW_REJECTED = "review_rejected"
    REWORK_REQUESTED = "rework_requested"
    TASK_FAILED = "task_failed"


class HandoffParty(str, Enum):
    ORCHESTRATOR = "orchestrator"
    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    VERIFIER = "verifier"
    REVIEWER = "reviewer"
    COMPLETION_GUARD = "completion_guard"
    HUMAN = "human"


class MailboxMessageStatus(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"


class HandoffEnvelope(BaseModel):
    """Versioned A2A message containing only small JSON data and artifact references."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    protocol_version: Literal["1.0"] = HANDOFF_PROTOCOL_VERSION
    message_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    trace_id: UUID
    correlation_id: UUID = Field(default_factory=uuid4)
    causation_id: UUID | None = None
    idempotency_key: str = Field(min_length=1, max_length=255)
    sender: HandoffParty
    recipient: HandoffParty
    type: HandoffType
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    artifacts: tuple[ArtifactReference, ...] = Field(
        default=(), max_length=MAX_HANDOFF_ARTIFACTS
    )
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("payload")
    @classmethod
    def validate_payload_size(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_HANDOFF_PAYLOAD_BYTES:
            raise ValueError(
                f"payload exceeds {MAX_HANDOFF_PAYLOAD_BYTES} bytes; use an artifact reference"
            )
        return value

    @model_validator(mode="after")
    def validate_route(self) -> "HandoffEnvelope":
        if self.sender is self.recipient:
            raise ValueError("sender and recipient must be different")
        if self.causation_id == self.message_id:
            raise ValueError("a message cannot cause itself")
        return self

