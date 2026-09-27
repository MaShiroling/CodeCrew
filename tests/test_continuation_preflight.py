"""Read-only HTTP preflight on real stores; no recovery, subprocess or models."""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.models import CreateTaskRequest, PostHumanMessageRequest
from app.main import create_app
from app.orchestration.models import TaskState
from app.team import MemberRole, MessageType
from app.team.budgets import ConversationBudgetGuard, ConversationBudgetPolicy
from tests import test_human_message_api as human_api
from tests.test_human_message_api import body
from tests.test_persistent_task_service import make_service

paused = human_api.paused


async def setup_preflight(context, role="planner"):
    service, view, room, loop = context
    guard = ConversationBudgetGuard(service.rooms)
    guard.initialize()
    loop.executor = SimpleNamespace(budget_guard=guard)
    receipt = await service.post_human_message(view.task_id, PostHumanMessageRequest(**body(view, recipient_role=role)))
    request = {"expected_revision": view.revision, "message_id": str(receipt.message.message_id),
               "target_role": "planner" if role == "orchestrator" else role}
    return service, view, room, loop, guard, request


def endpoint(view):
    return f"/api/v1/tasks/{view.task_id}/continue/preflight"


def stored_state(service, view, room):
    return (service.tasks.get(view.task_id), service.contexts.get(view.task_id),
            service.rooms.list_messages(room.room_id),
            service.router.trace_store.list(trace_id=view.trace_id, limit=1000))


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["planner", "implementer", "reviewer", "orchestrator"])
async def test_preflight_is_repeatable_read_only_and_never_execution_ready(paused, role):
    service, view, room, loop, guard, request = await setup_preflight(paused, role)
    before = stored_state(service, view, room)
    client = TestClient(create_app(task_service=service))
    response = client.post(endpoint(view), json=request)
    assert response.status_code == 200
    result = response.json()
    assert result["scope"] == "continuation-preflight" and result["checks_passed"]
    assert not result["execution_ready"] and not result["agent_dispatched"]
    assert result["trace_id"] == str(view.trace_id) and result["task_id"] == str(view.task_id)
    assert result["task_revision"] == view.revision
    assert result["target_role"] == request["target_role"]
    assert result["budget_usage"] == guard.usage(view.task_id, room_id=room.room_id).model_dump(mode="json")
    assert client.post(endpoint(view), json=request).json() == result
    assert stored_state(service, view, room) == before
    assert not service._runs and loop.received[0].task.state is TaskState.PLANNING


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    {"expected_revision": True}, {"expected_revision": "3"}, {"expected_revision": 0},
    {"message_id": "bad"}, {"target_role": "human"}, {"target_role": "verifier"},
    {"target_role": "orchestrator"}, {"content": "replace original message"},
    {"sender_id": str(uuid4())}, {"reset_budget": True}, {"state": "completed"},
    {"idempotency_key": str(uuid4())}, {"artifact_ids": []},
])
async def test_preflight_rejects_forged_or_malformed_body_without_writes(paused, updates):
    service, view, room, _, _, request = await setup_preflight(paused)
    before = stored_state(service, view, room)
    response = TestClient(create_app(task_service=service)).post(endpoint(view), json={**request, **updates})
    assert response.status_code == 422
    assert stored_state(service, view, room) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,code", [
    ("stale", 409), ("active", 409), ("cancelling", 409), ("closed", 409),
    ("unknown_message", 404), ("unknown_task", 404), ("wrong_target", 422),
    ("acked", 409), ("initial_issue", 422), ("agent_message", 422),
    ("non_api_human", 422), ("binding", 409),
    ("missing_guard", 503), ("missing_budget_table", 503), ("rework_exhausted", 409),
])
async def test_preconditions_fail_closed_without_consuming_intent(paused, fault, code):
    service, view, room, loop, _, request = await setup_preflight(paused)
    url = endpoint(view)
    if fault == "stale":
        request["expected_revision"] -= 1
    elif fault == "active":
        service._runs[view.task_id] = None
    elif fault == "cancelling":
        service._cancelling.add(view.task_id)
    elif fault == "closed":
        service.rooms.close_room(room.room_id)
    elif fault == "unknown_message":
        request["message_id"] = str(uuid4())
    elif fault == "unknown_task":
        url = f"/api/v1/tasks/{uuid4()}/continue/preflight"
    elif fault == "wrong_target":
        request["target_role"] = "implementer"
    elif fault == "acked":
        service.rooms.acknowledge(UUID(request["message_id"]), recipient_id=next(m.member_id for m in room.members if m.role is MemberRole.PLANNER))
    elif fault == "initial_issue":
        request["message_id"] = str(service.rooms.list_messages(room.room_id)[0].message.message_id)
    elif fault in {"agent_message", "non_api_human"}:
        item = human_api.parent_message(
            service, view, room, kind=MessageType.MESSAGE,
            sender_role=MemberRole.PLANNER if fault == "agent_message" else MemberRole.HUMAN,
            recipient_role=MemberRole.HUMAN if fault == "agent_message" else MemberRole.PLANNER,
        )
        request["message_id"] = str(item.message.message_id)
    elif fault == "binding":
        service.agent_names[MemberRole.PLANNER] = "different-agent"
    elif fault == "missing_guard":
        loop.executor = None
    elif fault == "missing_budget_table":
        with service.rooms.database.transaction() as connection:
            connection.execute("DROP TABLE agent_turn_usage")
    elif fault == "rework_exhausted":
        snapshot = service.tasks.get(view.task_id)
        snapshot.task.rework_rounds = 2
        saved = service.tasks.save(snapshot.task, expected_revision=snapshot.revision)
        request["expected_revision"] = saved.revision
    before = stored_state(service, view, room)
    response = TestClient(create_app(task_service=service)).post(url, json=request)
    assert response.status_code == code
    assert stored_state(service, view, room) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("limits", [
    {"max_agent_turns": 0}, {"max_reported_tokens": 0}, {"max_agent_duration_ms": 0},
    {"max_room_messages": 2}, {"max_repeated_messages": 1},
])
async def test_existing_conversation_limits_are_not_reset(paused, limits):
    service, view, room, _, guard, request = await setup_preflight(paused)
    guard.policy = ConversationBudgetPolicy(**limits)
    before = stored_state(service, view, room)
    response = TestClient(create_app(task_service=service)).post(endpoint(view), json=request)
    assert response.status_code == 409 and "budget" in response.json()["error"]["message"]
    assert stored_state(service, view, room) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [s for s in TaskState if s is not TaskState.NEEDS_HUMAN])
async def test_nonpaused_tasks_cannot_preflight(paused, state):
    service, view, room, _, _, request = await setup_preflight(paused)
    snapshot = service.tasks.get(view.task_id)
    snapshot.task.state = state  # Negative test only, not a supported transition.
    saved = service.tasks.save(snapshot.task, expected_revision=snapshot.revision)
    request["expected_revision"] = saved.revision
    before = stored_state(service, view, room)
    assert TestClient(create_app(task_service=service)).post(endpoint(view), json=request).status_code == 409
    assert stored_state(service, view, room) == before


@pytest.mark.asyncio
async def test_service_reconstruction_preserves_preflight_without_running_recovery(paused, tmp_path):
    service, view, room, _, _, request = await setup_preflight(paused)
    first = TestClient(create_app(task_service=service)).post(endpoint(view), json=request).json()
    restarted, loop = make_service(tmp_path)
    guard = ConversationBudgetGuard(restarted.rooms)
    guard.initialize()
    loop.executor = SimpleNamespace(budget_guard=guard)
    before = stored_state(service, view, room)
    response = TestClient(create_app(task_service=restarted)).post(endpoint(view), json=request)
    assert response.status_code == 200 and response.json() == first
    assert not restarted._runs and not loop.started.is_set()
    assert stored_state(service, view, room) == before


def test_preflight_openapi_and_missing_service():
    client = TestClient(create_app())
    schema = client.get("/openapi.json").json()
    assert "post" in schema["paths"]["/api/v1/tasks/{task_id}/continue/preflight"]
    assert "202" in schema["paths"]["/api/v1/tasks/{task_id}/continue"]["post"]["responses"]
    assert client.post(f"/api/v1/tasks/{uuid4()}/continue/preflight", json={}).status_code == 503


@pytest.mark.asyncio
async def test_cross_task_intent_is_not_a_continuation_source(paused):
    service, view, room, _, _, request = await setup_preflight(paused)
    created = await service.create_task(CreateTaskRequest(issue="Other task", repository_path=view.repository_path))
    await service.wait_for(created.task_id)
    snapshot = service.tasks.get(created.task_id)
    snapshot.task.transition_to(TaskState.NEEDS_HUMAN)
    service.tasks.save(snapshot.task, expected_revision=snapshot.revision)
    other = await service.get_task(created.task_id)
    receipt = await service.post_human_message(other.task_id, PostHumanMessageRequest(**body(other)))
    request["message_id"] = str(receipt.message.message_id)
    before = stored_state(service, view, room)
    assert TestClient(create_app(task_service=service)).post(endpoint(view), json=request).status_code == 404
    assert stored_state(service, view, room) == before


@pytest.mark.asyncio
async def test_accumulated_usage_and_unknown_token_count_are_preserved(paused):
    service, view, room, _, guard, request = await setup_preflight(paused)
    # Synthetic persisted usage, not a paid model call or claimed cost.
    with service.rooms.database.transaction() as connection:
        connection.execute(
            """INSERT INTO agent_turn_usage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid4()), str(view.task_id), str(view.trace_id), str(room.room_id),
             str(next(m.member_id for m in room.members if m.role is MemberRole.PLANNER)),
             "planner-fake", "planner", None, None, None, 1000, view.created_at.isoformat()),
        )
    before = stored_state(service, view, room)
    client = TestClient(create_app(task_service=service))
    result = client.post(endpoint(view), json=request).json()
    assert result["budget_usage"]["agent_turns"] == 1
    assert result["budget_usage"]["turns_without_token_usage"] == 1
    assert result["budget_usage"]["agent_duration_ms"] == 1000
    guard.policy = ConversationBudgetPolicy(max_agent_turns=1)
    assert client.post(endpoint(view), json=request).status_code == 409
    assert guard.usage(view.task_id, room_id=room.room_id).agent_turns == 1
    assert stored_state(service, view, room) == before


@pytest.mark.asyncio
async def test_human_answer_is_an_existing_correlated_intent(paused):
    service, view, room, _, _, _ = await setup_preflight(paused)
    parent = human_api.parent_message(service, view, room)
    request = body(view, reply_to=str(parent.message.message_id))
    request.pop("recipient_role")
    receipt = await service.post_human_message(view.task_id, PostHumanMessageRequest(**request))
    before = stored_state(service, view, room)
    response = TestClient(create_app(task_service=service)).post(endpoint(view), json={
        "expected_revision": view.revision, "message_id": str(receipt.message.message_id),
        "target_role": "planner",
    })
    assert response.status_code == 200
    assert response.json()["correlation_id"] == str(parent.message.correlation_id)
    assert stored_state(service, view, room) == before
