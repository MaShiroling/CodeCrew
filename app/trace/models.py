from enum import Enum
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from app.orchestration.models import utc_now


class TraceEventType(str, Enum):
    CHAT_MESSAGE_PERSISTED = "chat_message_persisted"
    WORKFLOW_DECISION = "workflow_decision"
    TASK_STATE_CHANGED = "task_state_changed"
    AGENT_TURN_STARTED = "agent_turn_started"
    AGENT_TURN_COMPLETED = "agent_turn_completed"
    AGENT_TURN_FAILED = "agent_turn_failed"
    AGENT_OUTPUT_RECORDED = "agent_output_recorded"
    AGENT_STREAM_RECORDED = "agent_stream_recorded"
    TEST_FAULT_INJECTED = "test_fault_injected"
    VERIFICATION_COMPLETED = "verification_completed"
    REVIEW_DECIDED = "review_decided"
    COMPLETION_DECIDED = "completion_decided"
    RECOVERY_DECIDED = "recovery_decided"
    BUDGET_EXCEEDED = "budget_exceeded"
    HUMAN_INPUT_REQUESTED = "human_input_requested"
    CONTINUATION_REQUESTED = "continuation_requested"
    CONTINUATION_CLAIMED = "continuation_claimed"
    CONTINUATION_SUCCEEDED = "continuation_succeeded"
    CONTINUATION_PAUSED = "continuation_paused"
    CONTINUATION_QUARANTINED = "continuation_quarantined"
    CONTINUATION_CANCEL_REQUESTED = "continuation_cancel_requested"
    CONTINUATION_CANCEL_OBSERVED = "continuation_cancel_observed"
    CONTINUATION_AUTHORIZED = "continuation_authorized"
    SYSTEM_ERROR = "system_error"


class TraceActorKind(str, Enum):
    AGENT = "agent"
    SYSTEM = "system"
    HUMAN = "human"
    DETERMINISTIC = "deterministic"


class TraceEvent(BaseModel):
    """Small immutable event; large evidence remains in ArtifactStore."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    event_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    trace_id: UUID
    type: TraceEventType
    actor_kind: TraceActorKind
    actor_id: str = Field(min_length=1, max_length=200)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    idempotency_key: str = Field(min_length=1, max_length=500)
    occurred_at: AwareDatetime = Field(default_factory=utc_now)


class StoredTraceEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(gt=0)
    event: TraceEvent
