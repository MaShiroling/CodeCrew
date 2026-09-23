import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from app.agents import (
    AgentCapability,
    AgentExitReason,
    AgentRegistry,
    AgentRole,
    FakeAgentAdapter,
    FakeAgentScenario,
    FakeEventSpec,
    PermissionMode,
)
from app.orchestration.models import Task, TaskState
from app.storage import ArtifactStore, ArtifactType, SQLiteDatabase
from app.team import (
    AgentTurnError,
    AgentTurnRunner,
    ChatActionError,
    ChatMessage,
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
)


def make_context(tmp_path: Path, scenario: FakeAgentScenario):
    database = SQLiteDatabase(tmp_path / "codecrew.sqlite3")
    artifacts = ArtifactStore(database, tmp_path / "artifacts")
    rooms = TeamRoomStore(database)
    artifacts.initialize()
    rooms.initialize()
    room_id = uuid4()

    def member(name: str, role: MemberRole, kind: MemberKind):
        return RoomMember(room_id=room_id, name=name, role=role, kind=kind)

    planner = member("planner", MemberRole.PLANNER, MemberKind.AGENT)
    implementer = member("implementer", MemberRole.IMPLEMENTER, MemberKind.AGENT)
    reviewer = member("reviewer", MemberRole.REVIEWER, MemberKind.AGENT)
    orchestrator = member("orchestrator", MemberRole.ORCHESTRATOR, MemberKind.SYSTEM)
    human = member("human", MemberRole.HUMAN, MemberKind.HUMAN)
    task = Task(issue="Implement a safe fallback", repository_path=str(tmp_path))
    room = TeamRoom(
        room_id=room_id,
        task_id=task.id,
        trace_id=task.trace_id,
        name="Agent turn test",
        members=(planner, implementer, reviewer, orchestrator, human),
    )
    rooms.create_room(room)
    router = ConversationRouter(rooms, artifacts)
    adapter = FakeAgentAdapter(
        scenario,
        name="fake-codex",
        capabilities=frozenset(
            {
                AgentCapability.CODE_EDIT,
                AgentCapability.STREAMING,
                AgentCapability.SESSION_RESUME,
            }
        ),
    )
    registry = AgentRegistry()
    registry.register(
        adapter,
        roles={AgentRole.IMPLEMENTER},
        permission_modes={PermissionMode.WORKSPACE_WRITE},
    )
    runner = AgentTurnRunner(registry, router)
    members = {
        MemberRole.PLANNER: planner,
        MemberRole.IMPLEMENTER: implementer,
        MemberRole.REVIEWER: reviewer,
        MemberRole.ORCHESTRATOR: orchestrator,
        MemberRole.HUMAN: human,
    }
    return runner, router, rooms, artifacts, adapter, task, room, members


def send_trigger(router, room, sender, recipient, **updates):
    values = {
        "room_id": room.room_id,
        "task_id": room.task_id,
        "trace_id": room.trace_id,
        "sender_id": sender.member_id,
        "recipients": (
            MessageRecipient(kind=RecipientKind.MEMBER, member_id=recipient.member_id),
        ),
        "type": MessageType.SYSTEM_EVENT,
        "content": "Start implementation",
        "idempotency_key": f"trigger-{uuid4()}",
    }
    values.update(updates)
    return router.route(
        ChatMessage(**values), authenticated_sender_id=sender.member_id
    )


@pytest.mark.asyncio
async def test_cancelling_turn_stops_adapter_and_preserves_pending_message(tmp_path: Path) -> None:
    runner, router, rooms, _, adapter, task, room, members = make_context(
        tmp_path, FakeAgentScenario(block_until_cancel=True)
    )
    implementer = members[MemberRole.IMPLEMENTER]
    trigger = send_trigger(router, room, members[MemberRole.ORCHESTRATOR], implementer)
    cancelled_sessions = []
    original_cancel = adapter.cancel

    async def tracked_cancel(session_id):
        cancelled_sessions.append(session_id)
        await original_cancel(session_id)

    adapter.cancel = tracked_cancel
    turn = asyncio.create_task(
        runner.run(
            task,
            room_id=room.room_id,
            member_id=implementer.member_id,
            agent_name=adapter.name,
            working_directory=tmp_path,
        )
    )
    for _ in range(100):
        if adapter.requests:
            break
        await asyncio.sleep(0.01)
    assert adapter.requests
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    assert len(cancelled_sessions) == 1
    assert rooms.pending_for(implementer.member_id) == (trigger,)


@pytest.mark.asyncio
async def test_turn_reads_messages_routes_actions_and_acks_after_success(
    tmp_path: Path,
) -> None:
    scenario = FakeAgentScenario(
        events=(FakeEventSpec(text="thinking"),),
        output={
            "actions": [
                {
                    "action": "ask_question",
                    "recipient": {"kind": "role", "role": "planner"},
                    "content": "Which fallback should I use?",
                },
                {"action": "finish_turn", "content": "Waiting for the planner"},
            ]
        },
    )
    runner, router, rooms, _, adapter, task, room, members = make_context(
        tmp_path, scenario
    )
    implementer = members[MemberRole.IMPLEMENTER]
    planner = members[MemberRole.PLANNER]
    trigger = send_trigger(
        router, room, members[MemberRole.ORCHESTRATOR], implementer
    )

    result = await runner.run(
        task,
        room_id=room.room_id,
        member_id=implementer.member_id,
        agent_name="fake-codex",
        working_directory=tmp_path,
    )

    assert result.consumed_message_ids == (trigger.message.message_id,)
    assert result.finish_summary == "Waiting for the planner"
    assert len(result.events) == 3
    assert len(result.routed_messages) == 1
    assert result.routed_messages[0].message.type is MessageType.QUESTION
    assert rooms.pending_for(implementer.member_id) == ()
    assert rooms.pending_for(planner.member_id) == result.routed_messages
    request = adapter.requests[0]
    assert request.role is AgentRole.IMPLEMENTER
    assert request.permission_mode is PermissionMode.WORKSPACE_WRITE
    assert str(trigger.message.message_id) in request.prompt


@pytest.mark.asyncio
async def test_answer_action_preserves_question_thread(tmp_path: Path) -> None:
    question_id = uuid4()
    scenario = FakeAgentScenario(
        output={
            "turn": {
                "actions": [
                    {
                        "action": "answer_question",
                        "recipient": {"kind": "role", "role": "planner"},
                        "content": "Use the existing default.",
                        "reply_to": str(question_id),
                    },
                    {"action": "finish_turn", "content": "Answered"},
                ]
            }
        }
    )
    runner, router, _, _, _, task, room, members = make_context(tmp_path, scenario)
    planner = members[MemberRole.PLANNER]
    implementer = members[MemberRole.IMPLEMENTER]
    question = send_trigger(
        router,
        room,
        planner,
        implementer,
        message_id=question_id,
        type=MessageType.QUESTION,
        content="Which fallback?",
    )

    result = await runner.run(
        task,
        room_id=room.room_id,
        member_id=implementer.member_id,
        agent_name="fake-codex",
        working_directory=tmp_path,
    )

    answer = result.routed_messages[0].message
    assert answer.type is MessageType.ANSWER
    assert answer.reply_to == question.message.message_id
    assert answer.correlation_id == question.message.correlation_id
    assert answer.causation_id == question.message.message_id


@pytest.mark.asyncio
async def test_share_artifact_resolves_integrity_bound_reference(tmp_path: Path) -> None:
    scenario = FakeAgentScenario()
    runner, router, rooms, artifacts, adapter, task, room, members = make_context(
        tmp_path, scenario
    )
    metadata = artifacts.put_json(
        {"patch": "evidence"},
        task_id=task.id,
        trace_id=task.trace_id,
        type=ArtifactType.DIFF,
        created_by="implementer",
        filename="patch.json",
    )
    adapter._scenario = FakeAgentScenario(
        output={
            "actions": [
                {
                    "action": "share_artifact",
                    "recipient": {"kind": "role", "role": "reviewer"},
                    "content": "Implementation patch",
                    "artifact_ids": [str(metadata.artifact_id)],
                },
                {"action": "finish_turn", "content": "Review requested"},
            ]
        }
    )
    implementer = members[MemberRole.IMPLEMENTER]
    send_trigger(router, room, members[MemberRole.ORCHESTRATOR], implementer)

    result = await runner.run(
        task,
        room_id=room.room_id,
        member_id=implementer.member_id,
        agent_name="fake-codex",
        working_directory=tmp_path,
    )

    shared = result.routed_messages[0].message
    assert shared.artifacts[0].artifact_id == metadata.artifact_id
    assert rooms.pending_for(members[MemberRole.REVIEWER].member_id)[0].message == shared


@pytest.mark.asyncio
async def test_invalid_or_failed_turn_does_not_ack_input(tmp_path: Path) -> None:
    invalid = FakeAgentScenario(output={"actions": [{"action": "send_message"}]})
    runner, router, rooms, _, _, task, room, members = make_context(tmp_path, invalid)
    implementer = members[MemberRole.IMPLEMENTER]
    trigger = send_trigger(
        router, room, members[MemberRole.ORCHESTRATOR], implementer
    )
    with pytest.raises(ChatActionError, match="invalid agent chat turn"):
        await runner.run(
            task,
            room_id=room.room_id,
            member_id=implementer.member_id,
            agent_name="fake-codex",
            working_directory=tmp_path,
        )
    assert rooms.pending_for(implementer.member_id)[0].message == trigger.message

    failed_scenario = FakeAgentScenario(
        reason=AgentExitReason.FAILED,
        exit_code=1,
        error="provider failed",
    )
    runner, router, rooms, _, _, task, room, members = make_context(
        tmp_path / "failed", failed_scenario
    )
    implementer = members[MemberRole.IMPLEMENTER]
    send_trigger(router, room, members[MemberRole.ORCHESTRATOR], implementer)
    with pytest.raises(AgentTurnError, match="provider failed"):
        await runner.run(
            task,
            room_id=room.room_id,
            member_id=implementer.member_id,
            agent_name="fake-codex",
            working_directory=tmp_path,
        )
    assert len(rooms.pending_for(implementer.member_id)) == 1


@pytest.mark.asyncio
async def test_turn_can_resume_native_agent_session(tmp_path: Path) -> None:
    scenario = FakeAgentScenario(
        output={"actions": [{"action": "finish_turn", "content": "Done"}]}
    )
    runner, router, _, _, adapter, task, room, members = make_context(tmp_path, scenario)
    implementer = members[MemberRole.IMPLEMENTER]
    send_trigger(router, room, members[MemberRole.ORCHESTRATOR], implementer)

    result = await runner.run(
        task,
        room_id=room.room_id,
        member_id=implementer.member_id,
        agent_name="fake-codex",
        working_directory=tmp_path,
        resume_native_session_id="native-thread-1",
    )

    assert result.session.native_session_id == "native-thread-1"
    assert adapter.requests[0].resume_from_session_id == "native-thread-1"


@pytest.mark.asyncio
async def test_planner_answers_clarification_and_publishes_versioned_plan(
    tmp_path: Path,
) -> None:
    scenario = FakeAgentScenario()
    runner, router, rooms, artifacts, _, task, room, members = make_context(
        tmp_path, scenario
    )
    planner = members[MemberRole.PLANNER]
    implementer = members[MemberRole.IMPLEMENTER]
    initial_metadata = artifacts.put_json(
        {"steps": ["use an unspecified fallback"]},
        task_id=task.id,
        trace_id=task.trace_id,
        type=ArtifactType.PLAN,
        created_by="planner",
        filename="plan-v1.json",
    )
    router.route(
        ChatMessage(
            room_id=room.room_id,
            task_id=task.id,
            trace_id=task.trace_id,
            sender_id=planner.member_id,
            recipients=(
                MessageRecipient(
                    kind=RecipientKind.MEMBER, member_id=implementer.member_id
                ),
            ),
            type=MessageType.PLAN_SHARED,
            content="Initial plan",
            artifacts=(
                artifacts.get_reference(
                    initial_metadata.artifact_id, summary="Initial plan"
                ),
            ),
            idempotency_key="initial-plan",
        ),
        authenticated_sender_id=planner.member_id,
    )
    question = router.route(
        ChatMessage(
            room_id=room.room_id,
            task_id=task.id,
            trace_id=task.trace_id,
            sender_id=implementer.member_id,
            recipients=(
                MessageRecipient(kind=RecipientKind.MEMBER, member_id=planner.member_id),
            ),
            type=MessageType.QUESTION,
            content="Which fallback should the implementation use?",
            idempotency_key="clarification-question",
        ),
        authenticated_sender_id=implementer.member_id,
    )
    planner_adapter = FakeAgentAdapter(
        FakeAgentScenario(
            output={
                "actions": [
                    {
                        "action": "answer_question",
                        "recipient": {"kind": "role", "role": "implementer"},
                        "content": "Use the repository's existing default.",
                        "reply_to": str(question.message.message_id),
                    },
                    {
                        "action": "share_plan",
                        "recipient": {"kind": "role", "role": "implementer"},
                        "content": "Plan revised after clarification",
                        "artifact_content": {
                            "steps": ["reuse existing default", "run regression tests"]
                        },
                    },
                    {"action": "finish_turn", "content": "Clarification resolved"},
                ]
            }
        ),
        name="fake-planner",
        capabilities=frozenset({AgentCapability.REPOSITORY_ANALYSIS}),
    )
    runner.registry.register(
        planner_adapter,
        roles={AgentRole.PLANNER},
        permission_modes={PermissionMode.READ_ONLY},
    )

    result = await runner.run(
        task,
        room_id=room.room_id,
        member_id=planner.member_id,
        agent_name="fake-planner",
        working_directory=tmp_path,
    )

    assert [item.message.type for item in result.routed_messages] == [
        MessageType.ANSWER,
        MessageType.PLAN_SHARED,
    ]
    revisions = rooms.list_plan_revisions(room.room_id)
    assert [revision.version for revision in revisions] == [1, 2]
    assert revisions[1].supersedes_artifact_id == initial_metadata.artifact_id
    assert revisions[1].addresses_message_ids == (question.message.message_id,)
    assert artifacts.get_metadata(revisions[1].artifact_id).metadata == {
        "plan_version": "2",
        "supersedes_artifact_id": str(initial_metadata.artifact_id),
    }
    assert str(initial_metadata.artifact_id) in planner_adapter.requests[0].prompt


@pytest.mark.asyncio
async def test_reviewer_rework_evidence_drives_implementer_back_to_verification(
    tmp_path: Path,
) -> None:
    implementer_scenario = FakeAgentScenario(
        output={
            "actions": [
                {
                    "action": "request_review",
                    "recipient": {"kind": "role", "role": "orchestrator"},
                    "content": "Rework completed and ready for verification",
                },
                {"action": "finish_turn", "content": "Fix submitted"},
            ]
        }
    )
    runner, router, rooms, artifacts, adapter, task, room, members = make_context(
        tmp_path, implementer_scenario
    )
    reviewer = members[MemberRole.REVIEWER]
    implementer = members[MemberRole.IMPLEMENTER]
    reviewer_adapter = FakeAgentAdapter(
        FakeAgentScenario(
            output={
                "actions": [
                    {
                        "action": "request_rework",
                        "recipient": {"kind": "role", "role": "implementer"},
                        "content": "The fallback path needs correction",
                        "artifact_content": {
                            "issues": [
                                {
                                    "priority": "high",
                                    "summary": "Fallback returns the wrong default",
                                    "resolved": False,
                                }
                            ]
                        },
                    },
                    {"action": "finish_turn", "content": "Rework required"},
                ]
            }
        ),
        name="fake-reviewer",
        capabilities=frozenset({AgentCapability.CODE_REVIEW}),
    )
    runner.registry.register(
        reviewer_adapter,
        roles={AgentRole.REVIEWER},
        permission_modes={PermissionMode.READ_ONLY},
    )
    send_trigger(
        router,
        room,
        members[MemberRole.ORCHESTRATOR],
        reviewer,
        content="Review the verified implementation",
    )
    for state in (
        TaskState.PLANNING,
        TaskState.IMPLEMENTING,
        TaskState.VERIFYING,
        TaskState.REVIEWING,
    ):
        task.transition_to(state)
    controller = WorkflowController(rooms)
    controller.initialize()

    review_turn = await runner.run(
        task,
        room_id=room.room_id,
        member_id=reviewer.member_id,
        agent_name="fake-reviewer",
        working_directory=tmp_path,
    )
    rework = review_turn.routed_messages[0]
    decision = controller.handle(task, rework)

    assert rework.message.type is MessageType.REWORK_REQUEST
    assert rework.message.artifacts[0].type is ArtifactType.REVIEW_REPORT
    review_content = artifacts.read_json(rework.message.artifacts[0].artifact_id)
    assert review_content["verdict"] == "rejected"
    assert review_content["issues"][0]["priority"] == "high"
    assert task.state is TaskState.IMPLEMENTING
    assert task.rework_rounds == 1
    assert decision.directives[0].target_role is MemberRole.IMPLEMENTER
    assert (
        rooms.pending_for(implementer.member_id)[0].message.artifacts[0].artifact_id
        == rework.message.artifacts[0].artifact_id
    )

    implementation_turn = await runner.run(
        task,
        room_id=room.room_id,
        member_id=implementer.member_id,
        agent_name=adapter.name,
        working_directory=tmp_path,
    )
    ready = implementation_turn.routed_messages[0]
    controller.handle(task, ready)

    assert ready.message.type is MessageType.IMPLEMENTATION_READY
    assert task.state is TaskState.VERIFYING
    assert str(rework.message.artifacts[0].artifact_id) in adapter.requests[0].prompt

    task.transition_to(TaskState.REVIEWING)
    send_trigger(
        router,
        room,
        members[MemberRole.ORCHESTRATOR],
        reviewer,
        content="Review the corrected implementation",
    )
    reviewer_adapter._scenario = FakeAgentScenario(
        output={
            "actions": [
                {
                    "action": "approve_review",
                    "recipient": {"kind": "role", "role": "orchestrator"},
                    "content": "The reported regression is fixed",
                    "artifact_content": {"issues": []},
                },
                {"action": "finish_turn", "content": "Approved"},
            ]
        }
    )
    with pytest.raises(AgentTurnError, match="carry forward"):
        await runner.run(
            task,
            room_id=room.room_id,
            member_id=reviewer.member_id,
            agent_name="fake-reviewer",
            working_directory=tmp_path,
        )

    issue = review_content["issues"][0]
    reviewer_adapter._scenario = FakeAgentScenario(
        output={
            "actions": [
                {
                    "action": "approve_review",
                    "recipient": {"kind": "role", "role": "orchestrator"},
                    "content": "The reported regression is fixed",
                    "artifact_content": {"issues": [{**issue, "resolved": True}]},
                },
                {"action": "finish_turn", "content": "Approved"},
            ]
        }
    )
    approval_turn = await runner.run(
        task,
        room_id=room.room_id,
        member_id=reviewer.member_id,
        agent_name="fake-reviewer",
        working_directory=tmp_path,
    )
    approval = approval_turn.routed_messages[0]

    assert approval.message.type is MessageType.REVIEW_APPROVED
    approved_content = artifacts.read_json(approval.message.artifacts[0].artifact_id)
    assert approved_content["issues"][0]["issue_id"] == issue["issue_id"]
    assert approved_content["issues"][0]["resolved"] is True
    assert issue["issue_id"] in reviewer_adapter.requests[-1].prompt
