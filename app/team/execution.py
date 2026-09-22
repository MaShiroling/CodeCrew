from collections import deque
from dataclasses import dataclass, field
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.agents import AgentRole
from app.config import get_settings
from app.orchestration.models import Task, TaskState
from app.storage import (
    AgentRuntimeBinding,
    ArtifactReference,
    ArtifactStore,
    ArtifactType,
    WorkflowRuntimeContext,
)
from app.team.budgets import (
    ConversationBudgetGuard,
    ConversationBudgetPolicy,
    ConversationBudgetViolation,
)
from app.team.controller import (
    WorkflowController,
    WorkflowDecision,
    WorkflowDirective,
    WorkflowDirectiveKind,
)
from app.team.models import (
    ChatMessage,
    MemberKind,
    MemberRole,
    MessageRecipient,
    MessageType,
    RecipientKind,
    RoomMember,
    StoredChatMessage,
)
from app.team.router import ConversationRouter
from app.team.turns import AgentTurnResult, AgentTurnRunner
from app.verification import (
    CompletionDecision,
    CompletionGuard,
    ReviewIssue,
    ReviewReport,
    ReviewVerdict,
    VerificationPlan,
    VerificationReport,
    Verifier,
)
from app.workspace import WorktreeHandle


class WorkflowExecutionError(RuntimeError):
    """Raised when a workflow directive cannot be executed safely."""


@dataclass(slots=True)
class WorkflowRuntime:
    task: Task
    room_id: UUID
    worktree: WorktreeHandle
    verification_plan: VerificationPlan
    agent_names: dict[MemberRole, str]
    native_session_ids: dict[MemberRole, str] = field(default_factory=dict)
    latest_verification: VerificationReport | None = None
    latest_completion: CompletionDecision | None = None

    def to_context(self) -> WorkflowRuntimeContext:
        return WorkflowRuntimeContext(
            task_id=self.task.id,
            trace_id=self.task.trace_id,
            room_id=self.room_id,
            worktree=self.worktree,
            verification_plan=self.verification_plan,
            agent_bindings=tuple(
                AgentRuntimeBinding(
                    role=AgentRole(role.value),
                    agent_name=name,
                    native_session_id=self.native_session_ids.get(role),
                )
                for role, name in sorted(
                    self.agent_names.items(), key=lambda item: item[0].value
                )
            ),
        )

    @classmethod
    def from_context(
        cls, task: Task, context: WorkflowRuntimeContext
    ) -> "WorkflowRuntime":
        if context.task_id != task.id or context.trace_id != task.trace_id:
            raise WorkflowExecutionError(
                "persisted runtime context belongs to another task or trace"
            )
        return cls(
            task=task,
            room_id=context.room_id,
            worktree=context.worktree,
            verification_plan=context.verification_plan,
            agent_names={
                MemberRole(binding.role.value): binding.agent_name
                for binding in context.agent_bindings
            },
            native_session_ids={
                MemberRole(binding.role.value): binding.native_session_id
                for binding in context.agent_bindings
                if binding.native_session_id is not None
            },
        )


class DirectiveExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    produced_events: tuple[StoredChatMessage, ...] = ()
    agent_turns: tuple[AgentTurnResult, ...] = ()
    paused: bool = False
    pause_reason: str | None = None


class WorkflowRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    task: Task
    decisions: tuple[WorkflowDecision, ...]
    agent_turns: tuple[AgentTurnResult, ...]
    processed_events: int
    paused: bool = False
    pause_reason: str | None = None
    limit_reached: bool = False


class WorkflowDirectiveExecutor:
    """Execute controller directives and publish deterministic system events."""

    def __init__(
        self,
        *,
        turns: AgentTurnRunner,
        router: ConversationRouter,
        verifier: Verifier,
        completion_guard: CompletionGuard,
        artifacts: ArtifactStore,
        budget_guard: ConversationBudgetGuard | None = None,
    ) -> None:
        databases = (
            turns.rooms.database.path,
            router.rooms.database.path,
            verifier.artifacts.database.path,
            completion_guard.artifacts.database.path,
        )
        if any(path != artifacts.database.path for path in databases):
            raise ValueError("workflow executor components must share one database")
        self.turns = turns
        self.router = router
        self.verifier = verifier
        self.completion_guard = completion_guard
        self.artifacts = artifacts
        self.budget_guard = budget_guard or ConversationBudgetGuard(
            turns.rooms,
            ConversationBudgetPolicy.from_settings(get_settings()),
        )
        self.budget_guard.initialize()

    async def execute(
        self,
        directive: WorkflowDirective,
        *,
        source: StoredChatMessage,
        runtime: WorkflowRuntime,
    ) -> DirectiveExecutionResult:
        if directive.kind is WorkflowDirectiveKind.WAKE_AGENT:
            if directive.target_role is None:
                raise WorkflowExecutionError("wake_agent directive requires target_role")
            member = self._member_for_role(runtime.room_id, directive.target_role)
            return await self._run_members((member,), runtime, source)
        if directive.kind is WorkflowDirectiveKind.WAKE_MEMBERS:
            members = tuple(
                self.turns.rooms.get_member(member_id)
                for member_id in directive.target_member_ids
            )
            human = next(
                (member for member in members if member.kind is MemberKind.HUMAN), None
            )
            if human is not None:
                return DirectiveExecutionResult(
                    paused=True,
                    pause_reason=f"waiting for human member {human.name}",
                )
            return await self._run_members(members, runtime, source)
        if directive.kind is WorkflowDirectiveKind.RUN_VERIFIER:
            return await self._run_verifier(source, runtime)
        if directive.kind is WorkflowDirectiveKind.RUN_COMPLETION_GUARD:
            return self._run_completion_guard(source, runtime)
        if directive.kind is WorkflowDirectiveKind.REQUEST_HUMAN:
            return DirectiveExecutionResult(
                paused=True,
                pause_reason=directive.reason,
            )
        return DirectiveExecutionResult()

    async def _run_members(
        self,
        members: tuple[RoomMember, ...],
        runtime: WorkflowRuntime,
        source: StoredChatMessage,
    ) -> DirectiveExecutionResult:
        turns: list[AgentTurnResult] = []
        events: list[StoredChatMessage] = []
        for member in members:
            if member.kind is not MemberKind.AGENT:
                raise WorkflowExecutionError(
                    f"cannot run a turn for non-Agent member {member.name}"
                )
            # Multiple messages produced by one Agent turn can each request a wake-up.
            # The first wake consumes the complete pending batch, so later directives
            # are intentionally coalesced instead of failing with "no pending messages".
            if not self.turns.rooms.pending_for(member.member_id, limit=1):
                continue
            violation = self.budget_guard.evaluate(
                runtime.task.id, room_id=runtime.room_id
            )
            if violation is not None:
                escalation = self._budget_pause(runtime, source, violation)
                return DirectiveExecutionResult(
                    produced_events=(*events, *escalation.produced_events),
                    agent_turns=tuple(turns),
                    paused=True,
                    pause_reason=escalation.pause_reason,
                )
            try:
                agent_name = runtime.agent_names[member.role]
            except KeyError as exc:
                raise WorkflowExecutionError(
                    f"no Agent is configured for role {member.role.value}"
                ) from exc
            turn = await self.turns.run(
                runtime.task,
                room_id=runtime.room_id,
                member_id=member.member_id,
                agent_name=agent_name,
                working_directory=runtime.worktree.worktree_path,
                resume_native_session_id=runtime.native_session_ids.get(member.role),
            )
            if turn.session.native_session_id is not None:
                runtime.native_session_ids[member.role] = turn.session.native_session_id
            turns.append(turn)
            events.extend(turn.routed_messages)
            self.budget_guard.record_turn(
                runtime.task,
                room_id=runtime.room_id,
                member_id=member.member_id,
                turn=turn,
            )
        return DirectiveExecutionResult(
            produced_events=tuple(events),
            agent_turns=tuple(turns),
        )

    def _budget_pause(
        self,
        runtime: WorkflowRuntime,
        source: StoredChatMessage,
        violation: ConversationBudgetViolation,
    ) -> DirectiveExecutionResult:
        if runtime.task.state is not TaskState.NEEDS_HUMAN:
            runtime.task.transition_to(TaskState.NEEDS_HUMAN)
        orchestrator = self._member_for_role(runtime.room_id, MemberRole.ORCHESTRATOR)
        human = self._member_for_role(runtime.room_id, MemberRole.HUMAN)
        event = self._publish_system_event(
            runtime,
            sender=orchestrator,
            recipient=human,
            type=MessageType.HUMAN_INPUT_REQUEST,
            content=(
                f"Conversation stopped: {violation.detail} "
                f"({violation.actual}/{violation.limit})"
            ),
            artifacts=(),
            source=source,
        )
        return DirectiveExecutionResult(
            produced_events=(event,),
            paused=True,
            pause_reason=event.message.content,
        )

    async def _run_verifier(
        self,
        source: StoredChatMessage,
        runtime: WorkflowRuntime,
    ) -> DirectiveExecutionResult:
        report = await self.verifier.verify(
            runtime.worktree,
            trace_id=runtime.task.trace_id,
            plan=runtime.verification_plan,
        )
        runtime.latest_verification = report
        verifier = self._member_for_role(runtime.room_id, MemberRole.VERIFIER)
        reviewer = self._member_for_role(runtime.room_id, MemberRole.REVIEWER)
        event = self._publish_system_event(
            runtime,
            sender=verifier,
            recipient=reviewer,
            type=MessageType.VERIFICATION_READY,
            content=(
                "Deterministic verification passed"
                if report.passed
                else "Deterministic verification failed"
            ),
            artifacts=(report.artifact,),
            source=source,
        )
        return DirectiveExecutionResult(produced_events=(event,))

    def _run_completion_guard(
        self,
        source: StoredChatMessage,
        runtime: WorkflowRuntime,
    ) -> DirectiveExecutionResult:
        if runtime.latest_verification is None:
            raise WorkflowExecutionError("completion guard has no verification report")
        review_reference = next(
            (
                reference
                for reference in source.message.artifacts
                if reference.type is ArtifactType.REVIEW_REPORT
            ),
            None,
        )
        if review_reference is None:
            raise WorkflowExecutionError("review approval has no review report artifact")
        review = self._load_review(review_reference, runtime.task)
        decision = self.completion_guard.evaluate(runtime.latest_verification, review)
        runtime.latest_completion = decision
        orchestrator = self._member_for_role(runtime.room_id, MemberRole.ORCHESTRATOR)
        recipient = self._member_for_role(
            runtime.room_id,
            MemberRole.HUMAN if decision.passed else MemberRole.IMPLEMENTER,
        )
        event = self._publish_system_event(
            runtime,
            sender=orchestrator,
            recipient=recipient,
            type=(
                MessageType.COMPLETION_PASSED
                if decision.passed
                else MessageType.COMPLETION_REJECTED
            ),
            content=(
                "Completion guard passed"
                if decision.passed
                else "Completion guard rejected the task"
            ),
            artifacts=(decision.artifact,),
            source=source,
        )
        return DirectiveExecutionResult(produced_events=(event,))

    def _load_review(self, reference: ArtifactReference, task: Task) -> ReviewReport:
        metadata = self.artifacts.get_metadata(reference.artifact_id)
        if metadata.task_id != task.id or metadata.trace_id != task.trace_id:
            raise WorkflowExecutionError("review artifact belongs to another task or trace")
        content = self.artifacts.read_json(reference.artifact_id)
        if not isinstance(content, dict):
            raise WorkflowExecutionError("review artifact is not a JSON object")
        try:
            return ReviewReport(
                task_id=content["task_id"],
                trace_id=content["trace_id"],
                reviewer=content["reviewer"],
                verdict=ReviewVerdict(content["verdict"]),
                issues=tuple(ReviewIssue.model_validate(item) for item in content["issues"]),
                summary=content["summary"],
                artifact=reference,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowExecutionError("review artifact has invalid content") from exc

    def _publish_system_event(
        self,
        runtime: WorkflowRuntime,
        *,
        sender: RoomMember,
        recipient: RoomMember,
        type: MessageType,
        content: str,
        artifacts: tuple[ArtifactReference, ...],
        source: StoredChatMessage,
    ) -> StoredChatMessage:
        return self.router.route(
            ChatMessage(
                room_id=runtime.room_id,
                task_id=runtime.task.id,
                trace_id=runtime.task.trace_id,
                sender_id=sender.member_id,
                recipients=(
                    MessageRecipient(
                        kind=RecipientKind.MEMBER,
                        member_id=recipient.member_id,
                    ),
                ),
                type=type,
                content=content,
                artifacts=artifacts,
                correlation_id=source.message.correlation_id,
                causation_id=source.message.message_id,
                idempotency_key=f"system:{type.value}:{source.message.message_id}",
            ),
            authenticated_sender_id=sender.member_id,
        )

    def _member_for_role(self, room_id: UUID, role: MemberRole) -> RoomMember:
        matches = tuple(
            member
            for member in self.turns.rooms.get_room(room_id).members
            if member.role is role
        )
        if len(matches) != 1:
            raise WorkflowExecutionError(
                f"expected exactly one {role.value} member, found {len(matches)}"
            )
        return matches[0]


class WorkflowEventLoop:
    """Continuously reduce and execute newly produced room events."""

    def __init__(
        self,
        controller: WorkflowController,
        executor: WorkflowDirectiveExecutor,
        *,
        max_events: int = 100,
    ) -> None:
        if max_events <= 0:
            raise ValueError("max_events must be positive")
        self.controller = controller
        self.executor = executor
        self.max_events = max_events

    async def run(
        self,
        runtime: WorkflowRuntime,
        initial_events: tuple[StoredChatMessage, ...],
    ) -> WorkflowRunResult:
        queue = deque(initial_events)
        decisions: list[WorkflowDecision] = []
        turns: list[AgentTurnResult] = []
        processed = 0
        paused = False
        pause_reason: str | None = None

        while queue and not runtime.task.is_terminal:
            if processed >= self.max_events:
                return WorkflowRunResult(
                    task=runtime.task,
                    decisions=tuple(decisions),
                    agent_turns=tuple(turns),
                    processed_events=processed,
                    paused=True,
                    pause_reason="workflow event limit reached",
                    limit_reached=True,
                )
            event = queue.popleft()
            decision = self.controller.handle(runtime.task, event)
            decisions.append(decision)
            processed += 1
            if decision.replayed:
                continue
            for directive in decision.directives:
                execution = await self.executor.execute(
                    directive,
                    source=event,
                    runtime=runtime,
                )
                turns.extend(execution.agent_turns)
                queue.extend(execution.produced_events)
                if execution.paused:
                    paused = True
                    pause_reason = execution.pause_reason
                    break
            if not paused:
                self._ack_orchestrator_deliveries(event, runtime.room_id)
            if paused:
                break

        return WorkflowRunResult(
            task=runtime.task,
            decisions=tuple(decisions),
            agent_turns=tuple(turns),
            processed_events=processed,
            paused=paused,
            pause_reason=pause_reason,
        )

    def _ack_orchestrator_deliveries(
        self, event: StoredChatMessage, room_id: UUID
    ) -> None:
        room = self.executor.turns.rooms.get_room(room_id)
        orchestrator_ids = {
            member.member_id
            for member in room.members
            if member.role is MemberRole.ORCHESTRATOR
        }
        for delivery in event.deliveries:
            if delivery.recipient_id in orchestrator_ids:
                self.executor.turns.rooms.acknowledge(
                    event.message.message_id,
                    recipient_id=delivery.recipient_id,
                )
