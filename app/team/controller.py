from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.orchestration.models import ALLOWED_TRANSITIONS, Task, TaskState
from app.storage import Migration
from app.team.models import MemberRole, MessageType, StoredChatMessage
from app.team.store import TeamRoomStore


class WorkflowControllerError(RuntimeError):
    """Raised when a room event is invalid for the task's current state."""


class WorkflowDirectiveKind(str, Enum):
    WAKE_AGENT = "wake_agent"
    WAKE_MEMBERS = "wake_members"
    RUN_VERIFIER = "run_verifier"
    RUN_COMPLETION_GUARD = "run_completion_guard"
    REQUEST_HUMAN = "request_human"
    NOOP = "noop"


class WorkflowDirective(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: WorkflowDirectiveKind
    target_role: MemberRole | None = None
    target_member_ids: tuple[UUID, ...] = ()
    reason: str


class WorkflowDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    message_id: UUID
    state_before: TaskState
    state_after: TaskState
    transitions: tuple[TaskState, ...] = ()
    rework_rounds_before: int
    rework_rounds_after: int
    directives: tuple[WorkflowDirective, ...]
    replayed: bool = False


WORKFLOW_CONTROLLER_MIGRATIONS = (
    Migration(
        version=4,
        name="create_processed_workflow_events",
        statements=(
            """
            CREATE TABLE processed_workflow_events (
                message_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                decision_json TEXT NOT NULL,
                processed_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX workflow_events_task_idx
            ON processed_workflow_events(task_id, processed_at)
            """,
        ),
    ),
)


class WorkflowController:
    """Reduce persisted room messages into legal task transitions and directives."""

    def __init__(self, rooms: TeamRoomStore, *, max_rework_rounds: int = 2) -> None:
        if max_rework_rounds < 0:
            raise ValueError("max_rework_rounds cannot be negative")
        self.rooms = rooms
        self.max_rework_rounds = max_rework_rounds

    def initialize(self) -> None:
        self.rooms.database.initialize(WORKFLOW_CONTROLLER_MIGRATIONS)

    def handle(self, task: Task, event: StoredChatMessage) -> WorkflowDecision:
        persisted = self.rooms.get_message(event.message.message_id)
        if persisted != event:
            raise WorkflowControllerError("workflow event does not match persisted message")
        if event.message.task_id != task.id or event.message.trace_id != task.trace_id:
            raise WorkflowControllerError("workflow event belongs to another task or trace")

        with self.rooms.database.transaction() as connection:
            existing = connection.execute(
                "SELECT decision_json FROM processed_workflow_events WHERE message_id = ?",
                (str(event.message.message_id),),
            ).fetchone()
            if existing is not None:
                decision = WorkflowDecision.model_validate_json(existing["decision_json"])
                self._replay_decision(task, decision)
                return decision.model_copy(update={"replayed": True})

            decision = self._reduce(task, event)
            connection.execute(
                """
                INSERT INTO processed_workflow_events(
                    message_id, task_id, decision_json, processed_at
                ) VALUES (?, ?, ?, datetime('now'))
                """,
                (
                    str(event.message.message_id),
                    str(task.id),
                    decision.model_dump_json(),
                ),
            )
        self._apply_decision(task, decision)
        return decision

    def _reduce(self, task: Task, event: StoredChatMessage) -> WorkflowDecision:
        message = event.message
        transitions: tuple[TaskState, ...] = ()
        rounds_after = task.rework_rounds
        directives: tuple[WorkflowDirective, ...]

        if message.type is MessageType.ISSUE_POSTED:
            self._require_state(task, TaskState.CREATED, message.type)
            transitions = (TaskState.PLANNING,)
            directives = (self._wake(MemberRole.PLANNER, "new issue requires a plan"),)
        elif message.type is MessageType.PLAN_SHARED:
            self._require_state(task, TaskState.PLANNING, message.type)
            transitions = (TaskState.IMPLEMENTING,)
            directives = (
                self._wake(MemberRole.IMPLEMENTER, "structured plan is available"),
            )
        elif message.type in {
            MessageType.IMPLEMENTATION_READY,
            MessageType.REVIEW_REQUEST,
        }:
            self._require_state(task, TaskState.IMPLEMENTING, message.type)
            transitions = (TaskState.VERIFYING,)
            directives = (
                WorkflowDirective(
                    kind=WorkflowDirectiveKind.RUN_VERIFIER,
                    reason="implementation requested review; deterministic verification runs first",
                ),
            )
        elif message.type is MessageType.VERIFICATION_READY:
            self._require_state(task, TaskState.VERIFYING, message.type)
            transitions = (TaskState.REVIEWING,)
            directives = (
                self._wake(MemberRole.REVIEWER, "verification evidence is ready"),
            )
        elif message.type is MessageType.REVIEW_APPROVED:
            self._require_state(task, TaskState.REVIEWING, message.type)
            directives = (
                WorkflowDirective(
                    kind=WorkflowDirectiveKind.RUN_COMPLETION_GUARD,
                    reason="review approval requires deterministic completion evaluation",
                ),
            )
        elif message.type is MessageType.COMPLETION_PASSED:
            self._require_state(task, TaskState.REVIEWING, message.type)
            transitions = (TaskState.COMPLETED,)
            directives = (
                WorkflowDirective(
                    kind=WorkflowDirectiveKind.NOOP,
                    reason="completion guard passed; task is complete",
                ),
            )
        elif message.type in {
            MessageType.COMPLETION_REJECTED,
            MessageType.REWORK_REQUEST,
        }:
            self._require_state(task, TaskState.REVIEWING, message.type)
            transitions, rounds_after, directives = self._rework_decision(task)
        elif message.type in {MessageType.QUESTION, MessageType.ANSWER}:
            directives = (
                WorkflowDirective(
                    kind=WorkflowDirectiveKind.WAKE_MEMBERS,
                    target_member_ids=tuple(
                        delivery.recipient_id for delivery in event.deliveries
                    ),
                    reason=f"new {message.type.value} requires a conversation turn",
                ),
            )
        elif message.type is MessageType.HUMAN_INPUT_REQUEST:
            directives = (
                WorkflowDirective(
                    kind=WorkflowDirectiveKind.REQUEST_HUMAN,
                    target_role=MemberRole.HUMAN,
                    target_member_ids=tuple(
                        delivery.recipient_id for delivery in event.deliveries
                    ),
                    reason="an Agent requested human input",
                ),
            )
        else:
            directives = (
                WorkflowDirective(
                    kind=WorkflowDirectiveKind.NOOP,
                    reason=f"{message.type.value} does not advance workflow state",
                ),
            )

        self._validate_transition_path(task.state, transitions)
        return WorkflowDecision(
            task_id=task.id,
            message_id=message.message_id,
            state_before=task.state,
            state_after=transitions[-1] if transitions else task.state,
            transitions=transitions,
            rework_rounds_before=task.rework_rounds,
            rework_rounds_after=rounds_after,
            directives=directives,
        )

    def _rework_decision(
        self, task: Task
    ) -> tuple[tuple[TaskState, ...], int, tuple[WorkflowDirective, ...]]:
        if task.rework_rounds >= self.max_rework_rounds:
            return (
                (TaskState.REWORK, TaskState.NEEDS_HUMAN),
                task.rework_rounds,
                (
                    WorkflowDirective(
                        kind=WorkflowDirectiveKind.REQUEST_HUMAN,
                        target_role=MemberRole.HUMAN,
                        reason="rework budget exhausted",
                    ),
                ),
            )
        next_round = task.rework_rounds + 1
        return (
            (TaskState.REWORK, TaskState.IMPLEMENTING),
            next_round,
            (
                self._wake(
                    MemberRole.IMPLEMENTER,
                    f"rework round {next_round} requested",
                ),
            ),
        )

    @staticmethod
    def _wake(role: MemberRole, reason: str) -> WorkflowDirective:
        return WorkflowDirective(
            kind=WorkflowDirectiveKind.WAKE_AGENT,
            target_role=role,
            reason=reason,
        )

    @staticmethod
    def _require_state(task: Task, expected: TaskState, message_type: MessageType) -> None:
        if task.state is not expected:
            raise WorkflowControllerError(
                f"{message_type.value} requires task state {expected.value}, "
                f"got {task.state.value}"
            )

    @staticmethod
    def _validate_transition_path(
        initial: TaskState, transitions: tuple[TaskState, ...]
    ) -> None:
        state = initial
        for target in transitions:
            if target not in ALLOWED_TRANSITIONS[state]:
                raise WorkflowControllerError(
                    f"illegal workflow transition from {state.value} to {target.value}"
                )
            state = target

    @staticmethod
    def _apply_decision(task: Task, decision: WorkflowDecision) -> None:
        task.rework_rounds = decision.rework_rounds_after
        for target in decision.transitions:
            task.transition_to(target)

    def _replay_decision(self, task: Task, decision: WorkflowDecision) -> None:
        if decision.task_id != task.id:
            raise WorkflowControllerError("persisted workflow decision belongs to another task")
        if task.state is decision.state_after:
            task.rework_rounds = decision.rework_rounds_after
            return
        states = (decision.state_before, *decision.transitions)
        try:
            index = states.index(task.state)
        except ValueError as exc:
            raise WorkflowControllerError(
                "task state diverged from the persisted workflow decision"
            ) from exc
        task.rework_rounds = decision.rework_rounds_after
        for target in decision.transitions[index:]:
            task.transition_to(target)
