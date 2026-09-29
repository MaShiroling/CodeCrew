"""Step 8.1: durable @mention discussion is distinct from workflow execution."""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.agents import AgentRegistry
from app.main import create_app
from app.orchestration.models import TaskState
from app.team import MemberRole, MessageType
from app.team.turns import AgentTurnError, AgentTurnRunner
from tests.test_human_message_api import parent_message

pytest_plugins = ("tests.test_human_message_api",)


def body(view, **updates):
    return {"expected_revision": view.revision, "idempotency_key": str(uuid4()),
            "content": "@白金 请和团队讨论实现风险", **updates}


@pytest.mark.asyncio
@pytest.mark.parametrize("text,roles", [
    ("@白金 请评估边界", ["planner"]),
    ("@KIMI 请评估实现", ["implementer"]),
    ("@whale 请评估风险", ["reviewer"]),
    ("@白金，@月见 请先讨论方案", ["planner", "implementer"]),
    ("@codex @白金 请讨论", ["planner"]),
])
async def test_discussion_routes_mention_without_waking_or_authorizing(paused, text, roles):
    service, view, room, _ = paused
    before = await service.get_task(view.task_id)
    context_before = service.contexts.get(view.task_id)
    targets = [member for member in room.members if member.role.value in roles]
    workflow_pending = {member.member_id: service.rooms.pending_for(
        member.member_id, exclude_discussion=True,
    ) for member in targets}
    endpoint = f"/api/v1/tasks/{view.task_id}/messages/discussion"
    with TestClient(create_app(task_service=service)) as client:
        request = body(view, content=text)
        response = client.post(endpoint, json=request)
        assert response.status_code == 201, response.text
        receipt = response.json()
        assert receipt["scope"] == "discussion"
        assert receipt["execution_authorized"] is False
        assert receipt["agent_dispatched"] is False
        assert receipt["task_revision"] == view.revision
        assert receipt["target_roles"] == roles
        message = receipt["message"]
        assert message["type"] == "discussion"
        assert message["pending_for_continuation"] is False
        assert set(message["recipient_ids"]) == {str(member.member_id) for member in targets}
        assert client.post(endpoint, json=request).json() == receipt
        assert client.get(f"/api/v1/tasks/{view.task_id}/messages").json()["items"][-1] == message
        preflight = client.post(f"/api/v1/tasks/{view.task_id}/continue/preflight", json={
            "expected_revision": view.revision,
            "message_id": message["message_id"], "target_role": roles[0],
        })
        assert preflight.status_code in {409, 422}
    for member in targets:
        assert service.rooms.pending_for(member.member_id, exclude_discussion=True) == workflow_pending[member.member_id]
        assert any(item.message.message_id == UUID(message["message_id"])
                   for item in service.rooms.pending_for(member.member_id))
    assert await service.get_task(view.task_id) == before
    assert service.contexts.get(view.task_id) == context_before
    assert not service._runs


@pytest.mark.asyncio
async def test_reply_inherits_agent_author_but_does_not_ack_or_execute(paused):
    service, view, room, _ = paused
    parent = parent_message(service, view, room)
    endpoint = f"/api/v1/tasks/{view.task_id}/messages/discussion"
    request = body(view, content="我认为应先确认测试边界", reply_to=str(parent.message.message_id))
    response = TestClient(create_app(task_service=service)).post(endpoint, json=request)
    assert response.status_code == 201, response.text
    message = response.json()["message"]
    stored = service.rooms.get_message(UUID(message["message_id"]))
    assert response.json()["target_roles"] == ["planner"]
    assert stored.message.reply_to == stored.message.causation_id == parent.message.message_id
    assert stored.message.correlation_id == parent.message.correlation_id
    assert all(delivery.status.value == "pending" for delivery in
               service.rooms.get_message(parent.message.message_id).deliveries)
    assert message["pending_for_continuation"] is False


@pytest.mark.asyncio
async def test_explicit_mention_adds_teammate_to_reply_author(paused):
    service, view, room, _ = paused
    parent = parent_message(service, view, room)
    response = TestClient(create_app(task_service=service)).post(
        f"/api/v1/tasks/{view.task_id}/messages/discussion",
        json=body(view, content="@月见 请回应白金的风险点", reply_to=str(parent.message.message_id)),
    )
    assert response.status_code == 201, response.text
    assert response.json()["target_roles"] == ["planner", "implementer"]
    assert response.json()["message"]["reply_to"] == str(parent.message.message_id)


@pytest.mark.asyncio
async def test_reply_to_human_or_missing_message_is_rejected(paused):
    service, view, room, _ = paused
    issue = service.rooms.list_messages(room.room_id)[0].message
    endpoint = f"/api/v1/tasks/{view.task_id}/messages/discussion"
    client = TestClient(create_app(task_service=service))
    before = service.rooms.list_messages(room.room_id)
    assert client.post(endpoint, json=body(view, reply_to=str(issue.message_id))).status_code == 422
    assert client.post(endpoint, json=body(view, reply_to=str(uuid4()))).status_code == 404
    assert service.rooms.list_messages(room.room_id) == before


@pytest.mark.asyncio
async def test_discussion_during_active_run_remains_outside_workflow_inputs(paused):
    service, view, room, _ = paused
    snapshot = service.tasks.get(view.task_id)
    snapshot.task.state = TaskState.PLANNING
    active = service.tasks.save(snapshot.task, expected_revision=snapshot.revision)
    planner = next(member for member in room.members if member.role is MemberRole.PLANNER)
    service._runs[view.task_id] = None  # Model a live owner without dispatching a model.
    before = service.rooms.pending_for(planner.member_id, exclude_discussion=True)
    response = TestClient(create_app(task_service=service)).post(
        f"/api/v1/tasks/{view.task_id}/messages/discussion",
        json=body(view, expected_revision=active.revision),
    )
    assert response.status_code == 201, response.text
    assert response.json()["agent_dispatched"] is False
    assert service.rooms.pending_for(planner.member_id, exclude_discussion=True) == before
    assert (await service.get_task(view.task_id)).revision == active.revision
    assert service._runs[view.task_id] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["没有收件人", "@unknown 请讨论", "@白金", "@白金 @bad 请讨论"])
async def test_invalid_mentions_do_not_write(paused, content):
    service, view, room, _ = paused
    before = service.rooms.list_messages(room.room_id)
    response = TestClient(create_app(task_service=service)).post(
        f"/api/v1/tasks/{view.task_id}/messages/discussion", json=body(view, content=content),
    )
    assert response.status_code == 422
    assert service.rooms.list_messages(room.room_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"recipient_role": "planner"}, {"execution_authorized": True},
    {"agent_dispatched": True}, {"type": "review_approved"},
    {"sender_role": "planner"}, {"artifact_ids": [str(uuid4())]},
    {"expected_revision": "1"},
])
async def test_discussion_rejects_authority_and_shape_spoofing(paused, extra):
    service, view, room, _ = paused
    before = service.rooms.list_messages(room.room_id)
    response = TestClient(create_app(task_service=service)).post(
        f"/api/v1/tasks/{view.task_id}/messages/discussion", json=body(view, **extra),
    )
    assert response.status_code == 422
    assert service.rooms.list_messages(room.room_id) == before


@pytest.mark.asyncio
async def test_discussion_idempotency_and_changed_request_conflict(paused):
    service, view, room, _ = paused
    endpoint = f"/api/v1/tasks/{view.task_id}/messages/discussion"
    request = body(view)
    client = TestClient(create_app(task_service=service))
    first = client.post(endpoint, json=request)
    assert first.status_code == 201
    assert client.post(endpoint, json=request).json() == first.json()
    changed = {**request, "content": "@月见 请讨论"}
    conflict = client.post(endpoint, json=changed)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "task_message_conflict"
    assert sum(item.message.type is MessageType.DISCUSSION for item in
               service.rooms.list_messages(room.room_id)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED])
async def test_final_tasks_reject_discussion(paused, state):
    service, view, room, _ = paused
    snapshot = service.tasks.get(view.task_id)
    snapshot.task.state = state
    saved = service.tasks.save(snapshot.task, expected_revision=snapshot.revision)
    before = service.rooms.list_messages(room.room_id)
    response = TestClient(create_app(task_service=service)).post(
        f"/api/v1/tasks/{view.task_id}/messages/discussion",
        json=body(view, expected_revision=saved.revision),
    )
    assert response.status_code == 409
    assert service.rooms.list_messages(room.room_id) == before


@pytest.mark.asyncio
async def test_selected_discussion_cannot_enter_normal_agent_turn(paused):
    service, view, room, _ = paused
    response = TestClient(create_app(task_service=service)).post(
        f"/api/v1/tasks/{view.task_id}/messages/discussion", json=body(view),
    )
    assert response.status_code == 201
    target = next(member for member in room.members if member.role is MemberRole.PLANNER)
    runner = AgentTurnRunner(AgentRegistry(), service.router)
    with pytest.raises(AgentTurnError, match="discussion is not an execution input"):
        await runner.run(
            service.tasks.get(view.task_id).task, room_id=room.room_id,
            member_id=target.member_id, agent_name="unused", working_directory=Path("."),
            input_message_ids=(UUID(response.json()["message"]["message_id"]),),
        )
    assert UUID(response.json()["message"]["message_id"]) not in {
        item.message.message_id for item in runner.rooms.pending_for(
            target.member_id, exclude_discussion=True,
        )
    }


def test_discussion_openapi_and_unconfigured_service():
    client = TestClient(create_app())
    path = "/api/v1/tasks/{task_id}/messages/discussion"
    operation = client.get("/openapi.json").json()["paths"][path]["post"]
    assert operation["requestBody"]["required"] and "201" in operation["responses"]
    assert client.post(f"/api/v1/tasks/{uuid4()}/messages/discussion", json={}).status_code == 503
