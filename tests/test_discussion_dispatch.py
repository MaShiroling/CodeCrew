"""8.2: bounded, read-only message-driven Agent discussion."""

import asyncio
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.agents import AgentRegistry, AgentRole, FakeAgentAdapter, FakeAgentScenario, PermissionMode
from app.api.models import CancelTaskRequest, PostDiscussionMessageRequest
from app.api.service import TaskStateConflict
from app.main import create_app
from app.orchestration.models import TaskState
from app.team import MemberRole, MessageType
from app.team.turns import AgentTurnRunner

pytest_plugins = ("tests.test_human_message_api",)


def discussion_output(*roles):
    return {"turn": {"actions": [
        *(
            {"action": "send_message", "recipient": {"kind": "role", "role": role},
             "content": f"Reply for {role}"}
            for role in roles
        ),
        {"action": "finish_turn", "content": "Discussion ended"},
    ]}}


def attach_runner(service, outputs):
    registry = AgentRegistry()
    adapters = {}
    for member_role, agent_role in (
        (MemberRole.PLANNER, AgentRole.PLANNER),
        (MemberRole.IMPLEMENTER, AgentRole.IMPLEMENTER),
        (MemberRole.REVIEWER, AgentRole.REVIEWER),
    ):
        adapter = FakeAgentAdapter(
            FakeAgentScenario(output=outputs.get(member_role, discussion_output("human"))),
            name=service.agent_names[member_role],
        )
        registry.register(
            adapter, roles={agent_role},
            permission_modes={PermissionMode.READ_ONLY, PermissionMode.WORKSPACE_WRITE},
        )
        adapters[member_role] = adapter
    service.discussion_turns = AgentTurnRunner(registry, service.router)
    return adapters


def request(view, content):
    return PostDiscussionMessageRequest(
        expected_revision=view.revision, idempotency_key=uuid4(), content=content,
    )


@pytest.mark.asyncio
async def test_http_discussion_receipt_and_room_reply(paused):
    service, view, _room, _ = paused
    attach_runner(service, {})
    app = create_app(task_service=service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/v1/tasks/{view.task_id}/messages/discussion", json={
            "expected_revision": view.revision, "idempotency_key": str(uuid4()),
            "content": "@白金 请说明风险",
        })
        assert response.status_code == 201, response.text
        assert response.json()["discussion_queued"] is True
        await asyncio.wait_for(service.wait_for_discussion(view.task_id), 3)
        page = await client.get(f"/api/v1/tasks/{view.task_id}/messages")
    assert page.status_code == 200
    assert any(item["sender_role"] == "planner" and item["type"] == "discussion"
               for item in page.json()["items"])


@pytest.mark.asyncio
async def test_human_mention_wakes_planner_then_teammate_without_coding(paused):
    service, view, room, _ = paused
    adapters = attach_runner(service, {
        MemberRole.PLANNER: discussion_output("human", "implementer"),
        MemberRole.IMPLEMENTER: discussion_output("human"),
    })
    before = service.tasks.get(view.task_id)
    receipt = await service.post_discussion_message(view.task_id, request(view, "@白金 和月见讨论风险"))
    assert receipt.discussion_queued and not receipt.agent_dispatched
    planner = next(member for member in room.members if member.role is MemberRole.PLANNER)
    assert service.rooms.pending_for(
        planner.member_id, only_discussion=True,
        correlation_id=receipt.message.correlation_id,
    )
    await asyncio.wait_for(service.wait_for_discussion(view.task_id), 3)
    messages = [item.message for item in service.rooms.list_messages(room.room_id)
                if item.message.type is MessageType.DISCUSSION]
    events = await service.list_trace_events(view.task_id, after_sequence=0, limit=100)
    assert len(messages) == 4, [(item.event.type.value, item.event.payload)
                                for item in events if item.event.type.value == "agent_turn_failed"]
    assert len(adapters[MemberRole.PLANNER].requests) == 1
    assert len(adapters[MemberRole.IMPLEMENTER].requests) == 1
    assert not adapters[MemberRole.REVIEWER].requests
    for adapter in adapters.values():
        for dispatched in adapter.requests:
            assert dispatched.discussion_only
            assert dispatched.permission_mode is PermissionMode.READ_ONLY
            assert not dispatched.clarification_only
    assert service.tasks.get(view.task_id) == before
    assert all(item.message.type is not MessageType.IMPLEMENTATION_READY
               for item in service.rooms.list_messages(room.room_id))


@pytest.mark.asyncio
async def test_execution_action_is_rejected_without_ack_or_workflow_change(paused):
    service, view, room, _ = paused
    adapters = attach_runner(service, {MemberRole.IMPLEMENTER: {
        "turn": {"actions": [
            {"action": "request_review", "recipient": {"kind": "role", "role": "orchestrator"},
             "content": "I changed code"},
            {"action": "finish_turn", "content": "done"},
        ]},
    }})
    before = service.tasks.get(view.task_id)
    receipt = await service.post_discussion_message(view.task_id, request(view, "@月见 请讨论如何修复"))
    await asyncio.wait_for(service.wait_for_discussion(view.task_id), 3)
    implementer = next(member for member in room.members if member.role is MemberRole.IMPLEMENTER)
    pending = service.rooms.pending_for(implementer.member_id, only_discussion=True)
    assert [item.message.message_id for item in pending] == [receipt.message.message_id]
    assert service.tasks.get(view.task_id) == before
    assert len(adapters[MemberRole.IMPLEMENTER].requests) == 1
    events = await service.list_trace_events(view.task_id, after_sequence=0, limit=100)
    assert any(event.event.type.value == "agent_turn_failed"
               and event.event.payload.get("mode") == "discussion" for event in events)


@pytest.mark.asyncio
async def test_reviewer_mention_is_a_readonly_chat_not_an_approval(paused):
    service, view, room, _ = paused
    adapters = attach_runner(service, {})
    await service.post_discussion_message(view.task_id, request(view, "@鲸鲸 请先讨论风险"))
    await asyncio.wait_for(service.wait_for_discussion(view.task_id), 3)
    reviewer_request = adapters[MemberRole.REVIEWER].requests[0]
    assert reviewer_request.permission_mode is PermissionMode.READ_ONLY
    assert reviewer_request.discussion_only and reviewer_request.output_schema is None
    assert not adapters[MemberRole.PLANNER].requests
    assert not adapters[MemberRole.IMPLEMENTER].requests
    assert not any(item.message.type is MessageType.REVIEW_APPROVED
                   for item in service.rooms.list_messages(room.room_id))


@pytest.mark.asyncio
async def test_discussion_does_not_advance_an_active_coding_task(paused):
    service, view, _room, _ = paused
    adapters = attach_runner(service, {})
    snapshot = service.tasks.get(view.task_id)
    snapshot.task.state = TaskState.PLANNING
    service.tasks.save(snapshot.task, expected_revision=snapshot.revision)
    active = await service.get_task(view.task_id)
    await service.post_discussion_message(view.task_id, request(active, "@白金 请解释方案"))
    await asyncio.wait_for(service.wait_for_discussion(view.task_id), 3)
    assert len(adapters[MemberRole.PLANNER].requests) == 1
    assert (await service.get_task(view.task_id)) == active


@pytest.mark.asyncio
async def test_agent_ping_pong_stops_at_six_turns(paused):
    service, view, room, _ = paused
    adapters = attach_runner(service, {
        MemberRole.PLANNER: discussion_output("implementer"),
        MemberRole.IMPLEMENTER: discussion_output("planner"),
    })
    original = request(view, "@白金 请和月见讨论")
    await service.post_discussion_message(view.task_id, original)
    await asyncio.wait_for(service.wait_for_discussion(view.task_id), 3)
    assert sum(len(adapter.requests) for adapter in adapters.values()) == 6
    events = await service.list_trace_events(view.task_id, after_sequence=0, limit=100)
    assert any(event.event.type.value == "budget_exceeded"
               and event.event.payload.get("mode") == "discussion" for event in events)
    assert (await service.get_task(view.task_id)).state is TaskState.NEEDS_HUMAN
    replay = await service.post_discussion_message(view.task_id, original)
    assert not replay.discussion_queued
    latest_agent = next(item.message for item in reversed(service.rooms.list_messages(room.room_id))
                        if item.message.type is MessageType.DISCUSSION
                        and item.message.sender_id != receipt_sender(room))
    followup = request(view, "继续讨论这个问题")
    followup = followup.model_copy(update={"reply_to": latest_agent.message_id})
    with pytest.raises(TaskStateConflict, match="discussion thread is blocked"):
        await service.post_discussion_message(view.task_id, followup)


@pytest.mark.asyncio
async def test_cancel_stops_active_discussion(paused):
    service, view, room, _ = paused
    snapshot = service.tasks.get(view.task_id)
    snapshot.task.state = TaskState.PLANNING
    active = service.tasks.save(snapshot.task, expected_revision=snapshot.revision)
    view = await service.get_task(view.task_id)
    registry = AgentRegistry()
    adapter = FakeAgentAdapter(FakeAgentScenario(block_until_cancel=True), name="planner-fake")
    registry.register(adapter, roles={AgentRole.PLANNER}, permission_modes={PermissionMode.READ_ONLY})
    service.discussion_turns = AgentTurnRunner(registry, service.router)
    await service.post_discussion_message(view.task_id, request(view, "@白金 请讨论"))
    async with asyncio.timeout(3):
        while not adapter.requests:
            await asyncio.sleep(0.01)
    cancelled = await service.cancel_task(view.task_id, CancelTaskRequest(expected_revision=active.revision))
    assert cancelled.state is TaskState.CANCELLED
    assert not service._discussion_runs
    assert not [item for item in service.rooms.list_messages(room.room_id)
                if item.message.type is MessageType.DISCUSSION
                and item.message.sender_id != receipt_sender(room)]


def receipt_sender(room):
    return next(member.member_id for member in room.members if member.role is MemberRole.HUMAN)
