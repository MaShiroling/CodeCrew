"""Pure contracts for opt-in, bounded discussion in standalone chat only."""

from datetime import timedelta
from enum import Enum
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.orchestration.models import utc_now
from app.team.models import MAX_CHAT_CONTENT_CHARS, MemberRole

_AGENT_ROLES = frozenset({MemberRole.PLANNER, MemberRole.IMPLEMENTER, MemberRole.REVIEWER})


class DiscussionRunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    AWAITING_HUMAN = "awaiting_human"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LIMIT_REACHED = "limit_reached"
    INTERRUPTED = "interrupted"


class DiscussionStopReason(str, Enum):
    HUMAN_PAUSED = "human_paused"
    HUMAN_INPUT_NEEDED = "human_input_needed"
    AGENT_FINISHED = "agent_finished"
    HUMAN_ENDED = "human_ended"
    AGENT_FAILED = "agent_failed"
    HUMAN_CANCELLED = "human_cancelled"
    TURN_LIMIT = "turn_limit"
    TIME_LIMIT = "time_limit"
    SERVER_RESTART = "server_restart"
    UNCERTAIN_RESULT = "uncertain_result"


class DiscussionNextAction(str, Enum):
    HANDOFF = "handoff"
    AWAIT_HUMAN = "await_human"
    FINISH = "finish"


_STOP_REASONS = {
    DiscussionRunStatus.PAUSED: frozenset({DiscussionStopReason.HUMAN_PAUSED}),
    DiscussionRunStatus.AWAITING_HUMAN: frozenset({DiscussionStopReason.HUMAN_INPUT_NEEDED}),
    DiscussionRunStatus.FINISHED: frozenset({
        DiscussionStopReason.AGENT_FINISHED, DiscussionStopReason.HUMAN_ENDED,
    }),
    DiscussionRunStatus.FAILED: frozenset({DiscussionStopReason.AGENT_FAILED}),
    DiscussionRunStatus.CANCELLED: frozenset({DiscussionStopReason.HUMAN_CANCELLED}),
    DiscussionRunStatus.LIMIT_REACHED: frozenset({
        DiscussionStopReason.TURN_LIMIT, DiscussionStopReason.TIME_LIMIT,
    }),
    DiscussionRunStatus.INTERRUPTED: frozenset({
        DiscussionStopReason.SERVER_RESTART, DiscussionStopReason.UNCERTAIN_RESULT,
    }),
}
_TERMINAL_STATUSES = frozenset(_STOP_REASONS) - {DiscussionRunStatus.PAUSED}
_ALLOWED_TRANSITIONS = {
    DiscussionRunStatus.CREATED: frozenset({
        DiscussionRunStatus.RUNNING, DiscussionRunStatus.CANCELLED,
        DiscussionRunStatus.INTERRUPTED,
    }),
    DiscussionRunStatus.RUNNING: frozenset({
        DiscussionRunStatus.PAUSED, DiscussionRunStatus.AWAITING_HUMAN,
        DiscussionRunStatus.FINISHED, DiscussionRunStatus.FAILED,
        DiscussionRunStatus.CANCELLED, DiscussionRunStatus.LIMIT_REACHED,
        DiscussionRunStatus.INTERRUPTED,
    }),
    DiscussionRunStatus.PAUSED: frozenset({
        DiscussionRunStatus.RUNNING, DiscussionRunStatus.FINISHED,
        DiscussionRunStatus.CANCELLED, DiscussionRunStatus.INTERRUPTED,
    }),
}


class DiscussionRunLimits(BaseModel):
    """Hard per-batch ceilings; a resumed batch never receives fresh limits."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_agent_turns: int = Field(default=8, ge=1, le=8)
    max_elapsed_seconds: int = Field(default=600, ge=30, le=600)


class DiscussionRun(BaseModel):
    """One opt-in chat batch; FINISHED never means a coding task succeeded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID = Field(default_factory=uuid4)
    room_id: UUID
    root_message_id: UUID
    correlation_id: UUID
    opening_role: MemberRole
    limits: DiscussionRunLimits = Field(default_factory=DiscussionRunLimits)
    status: DiscussionRunStatus = DiscussionRunStatus.CREATED
    stop_reason: DiscussionStopReason | None = None
    agent_turns_used: int = Field(default=0, ge=0)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    started_at: AwareDatetime | None = None
    stopped_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_run(self) -> "DiscussionRun":
        if self.opening_role not in _AGENT_ROLES:
            raise ValueError("discussion opening role must be an Agent")
        if self.agent_turns_used > self.limits.max_agent_turns:
            raise ValueError("discussion turn count exceeds its limit")
        if self.updated_at < self.created_at:
            raise ValueError("discussion updated_at cannot precede created_at")
        if self.started_at is not None and not (
            self.created_at <= self.started_at <= self.updated_at
        ):
            raise ValueError("discussion started_at is out of order")
        if self.stopped_at is not None and not (
            self.created_at <= self.stopped_at <= self.updated_at
        ):
            raise ValueError("discussion stopped_at is out of order")
        if (self.started_at is not None and self.stopped_at is not None
                and self.stopped_at < self.started_at):
            raise ValueError("discussion cannot stop before it starts")
        if self.agent_turns_used and self.started_at is None:
            raise ValueError("discussion turns require a started batch")
        if self.status is DiscussionRunStatus.CREATED and self.started_at is not None:
            raise ValueError("created discussion cannot already be started")
        if self.status in {
            DiscussionRunStatus.RUNNING, DiscussionRunStatus.PAUSED,
            DiscussionRunStatus.AWAITING_HUMAN, DiscussionRunStatus.FINISHED,
            DiscussionRunStatus.FAILED, DiscussionRunStatus.LIMIT_REACHED,
        } and self.started_at is None:
            raise ValueError("discussion status requires started_at")
        allowed_reasons = _STOP_REASONS.get(self.status)
        if allowed_reasons is None and self.stop_reason is not None:
            raise ValueError("active discussion cannot have stop_reason")
        if allowed_reasons is not None and self.stop_reason not in allowed_reasons:
            raise ValueError("discussion status and stop_reason do not match")
        if self.status in _TERMINAL_STATUSES and self.stopped_at is None:
            raise ValueError("terminal discussion requires stopped_at")
        if self.status not in _TERMINAL_STATUSES and self.stopped_at is not None:
            raise ValueError("active or paused discussion cannot have stopped_at")
        if (self.stop_reason is DiscussionStopReason.TURN_LIMIT
                and self.agent_turns_used != self.limits.max_agent_turns):
            raise ValueError("turn limit requires all Agent turns to be used")
        if (self.stop_reason is DiscussionStopReason.TIME_LIMIT
                and (self.started_at is None or self.stopped_at is None
                     or self.stopped_at - self.started_at < timedelta(
                         seconds=self.limits.max_elapsed_seconds
                     ))):
            raise ValueError("time limit requires the batch deadline to have passed")
        return self


def transition_discussion_run(
    run: DiscussionRun, status: DiscussionRunStatus, *,
    reason: DiscussionStopReason | None = None,
    at: AwareDatetime | None = None,
) -> DiscussionRun:
    """Validate one state transition without dispatching an Agent or writing storage."""
    if status not in _ALLOWED_TRANSITIONS.get(run.status, frozenset()):
        raise ValueError(f"invalid discussion transition: {run.status.value} -> {status.value}")
    now = at or utc_now()
    values = run.model_dump()
    values.update(status=status, stop_reason=reason, updated_at=now)
    if status is DiscussionRunStatus.RUNNING and run.started_at is None:
        values["started_at"] = now
    if status in _TERMINAL_STATUSES:
        values["stopped_at"] = now
    return DiscussionRun.model_validate(values)


def reserve_discussion_turn(
    run: DiscussionRun, *, at: AwareDatetime | None = None,
) -> DiscussionRun:
    """Count a reserved Agent turn; pause/resume cannot reset its budget."""
    if run.status is not DiscussionRunStatus.RUNNING:
        raise ValueError("discussion must be running to reserve an Agent turn")
    if run.agent_turns_used >= run.limits.max_agent_turns:
        raise ValueError("discussion Agent turn limit reached")
    now = at or utc_now()
    if run.started_at is None or now - run.started_at >= timedelta(
        seconds=run.limits.max_elapsed_seconds
    ):
        raise ValueError("discussion time limit reached")
    values = run.model_dump()
    values.update(agent_turns_used=run.agent_turns_used + 1, updated_at=now)
    return DiscussionRun.model_validate(values)


class DiscussionReply(BaseModel):
    """A chat-only decision; prose @mentions do not create handoffs."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    content: str = Field(min_length=1, max_length=MAX_CHAT_CONTENT_CHARS)
    next_action: DiscussionNextAction
    handoff_to: tuple[MemberRole, ...] = Field(default=(), max_length=2)

    @model_validator(mode="after")
    def validate_reply(self) -> "DiscussionReply":
        if not self.content.strip():
            raise ValueError("discussion reply content cannot be blank")
        if len(set(self.handoff_to)) != len(self.handoff_to):
            raise ValueError("discussion handoff targets must be unique")
        if any(role not in _AGENT_ROLES for role in self.handoff_to):
            raise ValueError("discussion handoff targets must be Agents")
        if self.next_action is DiscussionNextAction.HANDOFF and not self.handoff_to:
            raise ValueError("handoff action requires at least one teammate")
        if self.next_action is not DiscussionNextAction.HANDOFF and self.handoff_to:
            raise ValueError("non-handoff action cannot target teammates")
        return self

    def validate_for_speaker(self, role: MemberRole) -> "DiscussionReply":
        if role not in _AGENT_ROLES or role in self.handoff_to:
            raise ValueError("discussion Agent cannot hand off to itself")
        return self
