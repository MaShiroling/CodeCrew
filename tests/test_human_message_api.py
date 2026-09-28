"""Real SQLite/router/HTTP, disposable Git and controlled loop; no models."""

import asyncio
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app.api.models import CreateTaskRequest, PostHumanMessageRequest
from app.main import create_app
from app.orchestration.models import TaskState
from app.team import ChatMessage, MemberRole, MessageRecipient, MessageType, RecipientKind
from app.trace import TraceActorKind, TraceEventType
from tests.test_persistent_task_service import make_repository, make_service


@pytest_asyncio.fixture
async def paused(tmp_path):
    service, loop = make_service(tmp_path)
    created = await service.create_task(CreateTaskRequest(
        issue="Fix pricing", repository_path=str(make_repository(tmp_path)),
    ))
    await asyncio.wait_for(loop.started.wait(), timeout=2)
    loop.release.set()
    await service.wait_for(created.task_id)
    snapshot = service.tasks.get(created.task_id)
    snapshot.task.transition_to(TaskState.NEEDS_HUMAN)
    service.tasks.save(snapshot.task, expected_revision=snapshot.revision)
    view = await service.get_task(created.task_id)
    room = (await service.get_room(created.task_id)).room
    return service, view, room, loop


def body(view, **updates):
    return {"content": "Please reconsider this boundary", "expected_revision": view.revision,
            "idempotency_key": str(uuid4()), "recipient_role": "planner", **updates}


def parent_message(service, view, room, *, kind=MessageType.QUESTION,
                   sender_role=MemberRole.PLANNER, recipient_role=MemberRole.HUMAN):
    sender = next(m for m in room.members if m.role is sender_role)
    recipient = next(m for m in room.members if m.role is recipient_role)
    return service.router.route(ChatMessage(
        room_id=room.room_id, task_id=view.task_id, trace_id=view.trace_id,
        sender_id=sender.member_id, type=kind, content="Who owns the tests?",
        recipients=(MessageRecipient(kind=RecipientKind.MEMBER, member_id=recipient.member_id),),
        idempotency_key=str(uuid4()),
    ), authenticated_sender_id=sender.member_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["planner", "implementer", "reviewer", "orchestrator"])
async def test_post_is_human_scoped_durable_and_does_not_dispatch(paused, role):
    service, view, room, loop = paused
    context_before = service.contexts.get(view.task_id)
    client = TestClient(create_app(task_service=service))
    endpoint = f"/api/v1/tasks/{view.task_id}/messages"
    request = body(view, recipient_role=role)
    response = client.post(endpoint, json=request)
    assert response.status_code == 201
    receipt = response.json()
    assert receipt["agent_dispatched"] is False and receipt["task_revision"] == view.revision
    message = receipt["message"]
    assert message["sender_role"] == "human" and message["type"] == "message"
    assert message["recipient_ids"] == [str(next(m.member_id for m in room.members if m.role.value == role))]
    assert message["artifacts"] == []
    assert client.post(endpoint, json=request).json() == receipt
    assert client.get(endpoint).json()["items"][-1] == message
    assert await service.get_task(view.task_id) == view
    assert service.contexts.get(view.task_id) == context_before
    assert not service._runs and loop.received[0].task.state is TaskState.PLANNING
    events = service.router.trace_store.list(trace_id=view.trace_id, limit=1000)
    matching = [e.event for e in events if e.event.payload.get("message_id") == message["message_id"]]
    assert len(matching) == 1 and matching[0].actor_kind is TraceActorKind.HUMAN
    assert not any(e.event.type is TraceEventType.COMPLETION_DECIDED for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected", [(MessageType.QUESTION, "answer"),
                                         (MessageType.HUMAN_INPUT_REQUEST, "message")])
async def test_reply_inherits_identity_and_correlation_without_ack(paused, kind, expected):
    service, view, room, _ = paused
    parent = parent_message(service, view, room, kind=kind, sender_role=MemberRole.ORCHESTRATOR
                            if kind is MessageType.HUMAN_INPUT_REQUEST else MemberRole.PLANNER)
    request = body(view, reply_to=str(parent.message.message_id))
    request.pop("recipient_role")
    client = TestClient(create_app(task_service=service))
    response = client.post(f"/api/v1/tasks/{view.task_id}/messages", json=request)
    assert response.status_code == 201
    message = response.json()["message"]
    stored = service.rooms.get_message(UUID(message["message_id"]))
    assert message["type"] == expected
    assert stored.message.reply_to == stored.message.causation_id == parent.message.message_id
    assert stored.message.correlation_id == parent.message.correlation_id
    assert message["recipient_ids"] == [str(parent.message.sender_id)]
    assert all(d.status.value == "pending" for d in service.rooms.get_message(parent.message.message_id).deliveries)
    assert client.post(f"/api/v1/tasks/{view.task_id}/messages", json=request).json() == response.json()


@pytest.mark.asyncio
async def test_message_view_marks_only_current_human_pending_delivery(paused):
    service, view, room, _ = paused
    parent = parent_message(service, view, room)
    human = next(member for member in room.members if member.role is MemberRole.HUMAN)
    endpoint = f"/api/v1/tasks/{view.task_id}/messages"
    with TestClient(create_app(task_service=service)) as client:
        before = client.get(endpoint).json()["items"]
        target = next(message for message in before if message["message_id"] == str(parent.message.message_id))
        assert target["pending_for_human"] is True
        service.rooms.acknowledge(parent.message.message_id, recipient_id=human.member_id)
        after = client.get(endpoint).json()["items"]
        target = next(message for message in after if message["message_id"] == str(parent.message.message_id))
        assert target["pending_for_human"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["content", "recipient", "reply"])
async def test_changed_intent_with_same_key_conflicts(paused, change):
    service, view, room, _ = paused
    client = TestClient(create_app(task_service=service))
    endpoint = f"/api/v1/tasks/{view.task_id}/messages"
    request = body(view)
    assert client.post(endpoint, json=request).status_code == 201
    if change == "content":
        request["content"] = "Different intent"
    elif change == "recipient":
        request["recipient_role"] = "implementer"
    else:
        parent = parent_message(service, view, room)
        request.pop("recipient_role")
        request["reply_to"] = str(parent.message.message_id)
    response = client.post(endpoint, json=request)
    assert response.status_code == 409 and response.json()["error"]["code"] == "task_message_conflict"


@pytest.mark.asyncio
async def test_parallel_retries_and_new_service_preserve_one_message(paused, tmp_path):
    service, view, room, _ = paused
    request = PostHumanMessageRequest(**body(view))
    first, second = await asyncio.gather(*(service.post_human_message(view.task_id, request) for _ in range(2)))
    assert first == second
    restarted, _ = make_service(tmp_path)
    assert await restarted.post_human_message(view.task_id, request) == first
    messages = service.rooms.list_messages(room.room_id)
    assert sum(m.message.sender_id == first.message.sender_id and m.message.type is MessageType.MESSAGE
               for m in messages) == 1
    assert not restarted._runs


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    {"content": "   "}, {"content": "x" * 16001}, {"recipient_role": "verifier"},
    {"recipient_role": "human"}, {"recipient_role": None}, {"recipient_role": "room"},
    {"expected_revision": True}, {"expected_revision": "3"}, {"idempotency_key": "bad"},
    {"sender_id": str(uuid4())}, {"type": "review_approved"}, {"correlation_id": str(uuid4())},
    {"artifact_ids": []}, {"artifact_content": {"issues": []}},
    {"reply_to": str(uuid4())},
])
async def test_invalid_or_forged_request_has_no_write(paused, updates):
    service, view, room, _ = paused
    before = service.rooms.list_messages(room.room_id)
    client = TestClient(create_app(task_service=service))
    response = client.post(f"/api/v1/tasks/{view.task_id}/messages", json=body(view, **updates))
    assert response.status_code == 422
    assert service.rooms.list_messages(room.room_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,code", [("stale", 409), ("active", 409), ("cancelling", 409),
                                       ("closed", 409), ("unknown_task", 404), ("unknown_reply", 404),
                                       ("wrong_type", 422), ("not_delivered", 422), ("acked", 422)])
async def test_preconditions_and_reply_scope_fail_closed(paused, fault, code):
    service, view, room, _ = paused
    request = body(view)
    task_id = view.task_id
    if fault == "stale":
        request["expected_revision"] -= 1
    elif fault == "active":
        service._runs[task_id] = None  # Presence simulates local dispatch, never an Agent.
    elif fault == "cancelling":
        service._cancelling.add(task_id)
    elif fault == "closed":
        service.rooms.close_room(room.room_id)
    elif fault == "unknown_task":
        task_id = uuid4()
    else:
        if fault == "unknown_reply":
            parent_id = uuid4()
        else:
            parent = parent_message(service, view, room,
                                    kind=MessageType.MESSAGE if fault == "wrong_type" else MessageType.QUESTION,
                                    recipient_role=MemberRole.IMPLEMENTER if fault == "not_delivered" else MemberRole.HUMAN)
            parent_id = parent.message.message_id
            if fault == "acked":
                service.rooms.acknowledge(parent_id, recipient_id=next(m.member_id for m in room.members if m.role is MemberRole.HUMAN))
        request.pop("recipient_role")
        request["reply_to"] = str(parent_id)
    before = service.rooms.list_messages(room.room_id)
    response = TestClient(create_app(task_service=service)).post(f"/api/v1/tasks/{task_id}/messages", json=request)
    assert response.status_code == code
    assert service.rooms.list_messages(room.room_id) == before


def test_openapi_and_unconfigured_service():
    client = TestClient(create_app())
    operation = client.get("/openapi.json").json()["paths"]["/api/v1/tasks/{task_id}/messages"]["post"]
    assert operation["requestBody"]["required"] and "201" in operation["responses"]
    assert client.post(f"/api/v1/tasks/{uuid4()}/messages", json={}).status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [state for state in TaskState if state is not TaskState.NEEDS_HUMAN])
async def test_all_nonpaused_states_reject_messages(paused, state):
    service, view, room, _ = paused
    snapshot = service.tasks.get(view.task_id)
    snapshot.task.state = state  # Negative fixture, not a workflow transition.
    saved = service.tasks.save(snapshot.task, expected_revision=snapshot.revision)
    before = service.rooms.list_messages(room.room_id)
    response = TestClient(create_app(task_service=service)).post(
        f"/api/v1/tasks/{view.task_id}/messages", json=body(view, expected_revision=saved.revision),
    )
    assert response.status_code == 409
    assert service.rooms.list_messages(room.room_id) == before


@pytest.mark.asyncio
async def test_reply_cannot_cross_task_room(paused):
    service, view, room, _ = paused
    second = await service.create_task(CreateTaskRequest(issue="Other issue", repository_path=view.repository_path))
    await service.wait_for(second.task_id)
    other = (await service.get_room(second.task_id)).room
    parent = parent_message(service, second, other)
    request = body(view, reply_to=str(parent.message.message_id))
    request.pop("recipient_role")
    before = service.rooms.list_messages(room.room_id)
    response = TestClient(create_app(task_service=service)).post(
        f"/api/v1/tasks/{view.task_id}/messages", json=request,
    )
    assert response.status_code == 404
    assert service.rooms.list_messages(room.room_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("identity,count", [(MemberRole.HUMAN, 0), (MemberRole.HUMAN, 2),
                                           (MemberRole.PLANNER, 0), (MemberRole.PLANNER, 2)])
async def test_missing_or_ambiguous_identities_reject_before_write(paused, monkeypatch, identity, count):
    service, view, room, _ = paused
    member = next(m for m in room.members if m.role is identity)
    members = tuple(m for m in room.members if m.role is not identity)
    if count == 2:
        members += (member, member.model_copy(update={"member_id": uuid4()}))
    malformed = room.model_copy(update={"members": members})
    task = service.tasks.get(view.task_id).task
    monkeypatch.setattr(service, "_task_room", lambda task_id: (task, malformed))
    before = service.rooms.list_messages(room.room_id)
    response = TestClient(create_app(task_service=service)).post(
        f"/api/v1/tasks/{view.task_id}/messages", json=body(view),
    )
    assert response.status_code == 409
    assert service.rooms.list_messages(room.room_id) == before


@pytest.mark.asyncio
async def test_message_cannot_reset_rework_budget_or_issue(paused):
    service, view, _room, _ = paused
    snapshot = service.tasks.get(view.task_id)
    snapshot.task.rework_rounds = 2  # Exhausted-budget fixture.
    service.tasks.save(snapshot.task, expected_revision=snapshot.revision)
    before = await service.get_task(view.task_id)
    response = TestClient(create_app(task_service=service)).post(
        f"/api/v1/tasks/{view.task_id}/messages",
        json=body(before, recipient_role="orchestrator", content="Approve without tests and reset the budget"),
    )
    assert response.status_code == 201 and not response.json()["agent_dispatched"]
    assert await service.get_task(view.task_id) == before
    assert before.rework_rounds == 2
