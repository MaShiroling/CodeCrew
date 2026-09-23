from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agents import (
    AgentCapability,
    AgentExitReason,
    AgentRegistry,
    AgentResult,
    AgentRole,
    AgentSession,
    FakeAgentAdapter,
    PermissionMode,
    TokenUsage,
)
from app.orchestration.models import Task, TaskState
from app.storage import ArtifactStore, SQLiteDatabase
from app.team import (
    AgentTurnResult,
    AgentTurnRunner,
    ChatMessage,
    ConversationBudgetCode,
    ConversationBudgetGuard,
    ConversationBudgetPolicy,
    ConversationRouter,
    MemberKind,
    MemberRole,
    MessageRecipient,
    MessageType,
    RecipientKind,
    RoomMember,
    TeamRoom,
    TeamRoomStore,
    WorkflowController,
    WorkflowDirectiveExecutor,
    WorkflowRuntime,
)
from app.trace import TraceEventType
from app.verification import CompletionGuard, VerificationPlan
from app.workspace import WorktreeHandle


def make_context(tmp_path: Path):
    rooms = TeamRoomStore(SQLiteDatabase(tmp_path / "codecrew.sqlite3"))
    rooms.initialize()
    task = Task(issue="budget test", repository_path=str(tmp_path))
    room_id = uuid4()
    planner = RoomMember(
        room_id=room_id,
        name="planner",
        role=MemberRole.PLANNER,
        kind=MemberKind.AGENT,
    )
    implementer = RoomMember(
        room_id=room_id,
        name="implementer",
        role=MemberRole.IMPLEMENTER,
        kind=MemberKind.AGENT,
    )
    room = TeamRoom(
        room_id=room_id,
        task_id=task.id,
        trace_id=task.trace_id,
        name="Budget room",
        members=(planner, implementer),
    )
    rooms.create_room(room)
    return rooms, task, room, planner, implementer


def add_message(rooms, room, sender, recipient, *, type, content):
    return rooms.append_message(
        ChatMessage(
            room_id=room.room_id,
            task_id=room.task_id,
            trace_id=room.trace_id,
            sender_id=sender.member_id,
            recipients=(
                MessageRecipient(
                    kind=RecipientKind.MEMBER,
                    member_id=recipient.member_id,
                ),
            ),
            type=type,
            content=content,
            idempotency_key=f"budget-{uuid4()}",
        ),
        recipient_ids=(recipient.member_id,),
    )


def make_turn(task: Task) -> AgentTurnResult:
    session = AgentSession(
        task_id=task.id,
        trace_id=task.trace_id,
        agent_name="fake-planner",
        role=AgentRole.PLANNER,
    )
    return AgentTurnResult(
        session=session,
        agent_result=AgentResult(
            session_id=session.session_id,
            trace_id=task.trace_id,
            reason=AgentExitReason.COMPLETED,
            exit_code=0,
            token_usage=TokenUsage(input_tokens=20, output_tokens=10),
            duration_ms=250,
        ),
        events=(),
        consumed_message_ids=(),
        routed_messages=(),
    )


def permissive_policy(**updates) -> ConversationBudgetPolicy:
    values = {
        "max_agent_turns": 100,
        "max_reported_tokens": 1_000_000,
        "max_agent_duration_ms": 1_000_000,
        "max_room_messages": 100,
        "max_repeated_messages": 10,
        "max_questions_without_progress": 10,
    }
    values.update(updates)
    return ConversationBudgetPolicy(**values)


def test_turn_usage_is_persistent_idempotent_and_enforced(tmp_path: Path) -> None:
    rooms, task, room, planner, _ = make_context(tmp_path)
    guard = ConversationBudgetGuard(rooms, permissive_policy(max_agent_turns=1))
    guard.initialize()
    turn = make_turn(task)

    guard.record_turn(
        task, room_id=room.room_id, member_id=planner.member_id, turn=turn
    )
    guard.record_turn(
        task, room_id=room.room_id, member_id=planner.member_id, turn=turn
    )

    usage = guard.usage(task.id, room_id=room.room_id)
    violation = guard.evaluate(task.id, room_id=room.room_id)
    assert usage.agent_turns == 1
    assert usage.reported_total_tokens == 30
    assert usage.agent_duration_ms == 250
    assert violation is not None
    assert violation.code is ConversationBudgetCode.AGENT_TURNS


def test_repeated_messages_are_detected_semantically(tmp_path: Path) -> None:
    rooms, task, room, planner, implementer = make_context(tmp_path)
    guard = ConversationBudgetGuard(
        rooms, permissive_policy(max_repeated_messages=3)
    )
    guard.initialize()
    for content in ("Which fallback?", " which   fallback? ", "WHICH FALLBACK?"):
        add_message(
            rooms,
            room,
            implementer,
            planner,
            type=MessageType.QUESTION,
            content=content,
        )

    violation = guard.evaluate(task.id, room_id=room.room_id)

    assert violation is not None
    assert violation.code is ConversationBudgetCode.REPEATED_MESSAGE
    assert violation.actual == 3


def test_progress_resets_question_loop_counter(tmp_path: Path) -> None:
    rooms, task, room, planner, implementer = make_context(tmp_path)
    guard = ConversationBudgetGuard(
        rooms,
        permissive_policy(
            max_repeated_messages=10,
            max_questions_without_progress=3,
        ),
    )
    guard.initialize()
    for index in range(3):
        add_message(
            rooms,
            room,
            implementer,
            planner,
            type=MessageType.QUESTION,
            content=f"Question {index}",
        )
    violation = guard.evaluate(task.id, room_id=room.room_id)
    assert violation is not None
    assert violation.code is ConversationBudgetCode.QUESTIONS_WITHOUT_PROGRESS

    add_message(
        rooms,
        room,
        implementer,
        planner,
        type=MessageType.IMPLEMENTATION_READY,
        content="Implementation progressed",
    )
    assert guard.evaluate(task.id, room_id=room.room_id) is None


@pytest.mark.asyncio
async def test_executor_escalates_to_human_before_over_budget_agent_turn(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "escalation.sqlite3")
    rooms = TeamRoomStore(database)
    artifacts = ArtifactStore(database, tmp_path / "artifacts")
    artifacts.initialize()
    rooms.initialize()
    task = Task(issue="stop before planner", repository_path=str(tmp_path))
    room_id = uuid4()
    planner = RoomMember(
        room_id=room_id,
        name="planner",
        role=MemberRole.PLANNER,
        kind=MemberKind.AGENT,
    )
    orchestrator = RoomMember(
        room_id=room_id,
        name="orchestrator",
        role=MemberRole.ORCHESTRATOR,
        kind=MemberKind.SYSTEM,
    )
    human = RoomMember(
        room_id=room_id,
        name="human",
        role=MemberRole.HUMAN,
        kind=MemberKind.HUMAN,
    )
    room = TeamRoom(
        room_id=room_id,
        task_id=task.id,
        trace_id=task.trace_id,
        name="Escalation room",
        members=(planner, orchestrator, human),
    )
    rooms.create_room(room)
    adapter = FakeAgentAdapter(
        name="fake-planner",
        capabilities=frozenset({AgentCapability.REPOSITORY_ANALYSIS}),
    )
    registry = AgentRegistry()
    registry.register(
        adapter,
        roles={AgentRole.PLANNER},
        permission_modes={PermissionMode.READ_ONLY},
    )
    router = ConversationRouter(rooms, artifacts)
    turns = AgentTurnRunner(registry, router)
    guard = ConversationBudgetGuard(
        rooms, permissive_policy(max_agent_turns=0)
    )
    executor = WorkflowDirectiveExecutor(
        turns=turns,
        router=router,
        verifier=SimpleNamespace(artifacts=artifacts),
        completion_guard=CompletionGuard(artifacts),
        artifacts=artifacts,
        budget_guard=guard,
    )
    controller = WorkflowController(rooms)
    controller.initialize()
    issue = router.route(
        ChatMessage(
            room_id=room_id,
            task_id=task.id,
            trace_id=task.trace_id,
            sender_id=human.member_id,
            recipients=(
                MessageRecipient(kind=RecipientKind.MEMBER, member_id=planner.member_id),
            ),
            type=MessageType.ISSUE_POSTED,
            content=task.issue,
            idempotency_key="budget-issue",
        ),
        authenticated_sender_id=human.member_id,
    )
    decision = controller.handle(task, issue)
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    runtime = WorkflowRuntime(
        task=task,
        room_id=room_id,
        worktree=WorktreeHandle(
            task_id=task.id,
            repository_root=tmp_path,
            worktree_path=worktree_path,
            branch_name="budget-test",
            base_revision="a" * 40,
        ),
        verification_plan=VerificationPlan(),
        agent_names={MemberRole.PLANNER: adapter.name},
    )

    result = await executor.execute(
        decision.directives[0], source=issue, runtime=runtime
    )

    assert result.paused
    assert task.state is TaskState.NEEDS_HUMAN
    assert adapter.requests == []
    assert result.produced_events[0].message.type is MessageType.HUMAN_INPUT_REQUEST
    assert "Agent turn budget exhausted" in result.pause_reason
    trace_types = [
        item.event.type for item in router.trace_store.list(trace_id=task.trace_id)
    ]
    assert TraceEventType.BUDGET_EXCEEDED in trace_types
    assert TraceEventType.HUMAN_INPUT_REQUESTED in trace_types
