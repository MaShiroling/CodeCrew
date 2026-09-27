"""Same-loop HTTP admission/dispatch tests; Fake models, real Git and SQLite."""

import asyncio
import sqlite3
import subprocess
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.agents import FakeAgentScenario, PermissionMode
from app.api.models import CreateTaskRequest
from app.main import create_app
from app.orchestration.models import TaskState
from app.storage.continuations import ContinuationState
from app.team.budgets import ConversationBudgetPolicy
from app.trace import TraceEventType
from tests import test_continuation_runtime as runtime_tests
from tests import test_controlled_resumption as resumption_tests
from tests.test_task_api_e2e import make_repository, make_runtime

paused = runtime_tests.paused


def client(service):
    return AsyncClient(transport=ASGITransport(app=create_app(task_service=service)), base_url="http://test")


def url(view):
    return f"/api/v1/tasks/{view.task_id}/continue"


async def command(paused, role="planner"):
    _, request = await runtime_tests.intent(paused, recipient=role)
    return {**request.model_dump(mode="json"), "idempotency_key": str(uuid4())}


async def granted(paused, role="planner"):
    _, grant = await resumption_tests.authorized(paused, role)
    return {"expected_revision": grant.intent.expected_revision, "message_id": str(grant.intent.message_id),
            "target_role": role, "idempotency_key": str(grant.intent.idempotency_key),
            "authorization_id": str(grant.authorization_id)}


async def finished(service, request_id):
    owned = service._continuation_runs.get(UUID(str(request_id)))
    if owned is not None:
        await asyncio.wait_for(asyncio.gather(owned[1], return_exceptions=True), timeout=5)
    # Done callback has been registered before gather, so it runs first.
    return service.continuations.get(UUID(str(request_id)))


@pytest_asyncio.fixture
async def production_paused(tmp_path):
    runtime, agents = make_runtime(tmp_path)
    agents[2]._scenario = FakeAgentScenario(output={"actions": [
        {"action": "request_human_input", "recipient": {"kind": "role", "role": "human"},
         "content": "Please clarify compatibility"},
        {"action": "finish_turn", "content": "Waiting"},
    ]})
    repository = make_repository(tmp_path)
    (repository / "verify.py").write_text("from src.app import value\nassert value == 2\nprint('verified')\n")
    await asyncio.to_thread(subprocess.run, ["git", "add", "verify.py"], cwd=repository, check=True, capture_output=True)
    await asyncio.to_thread(subprocess.run, ["git", "-c", "user.name=CodeCrew Tests", "-c", "user.email=tests@codecrew.invalid",
                    "commit", "-m", "verification log"], cwd=repository, check=True, capture_output=True)
    async with client(runtime.service) as api:
        created = await api.post("/api/v1/tasks", json={"issue": "Set value to two", "repository_path": str(repository)})
        assert created.status_code == 201
    await asyncio.wait_for(runtime.service.wait_for(UUID(created.json()["task_id"])), timeout=10)
    view = await runtime.service.get_task(UUID(created.json()["task_id"]))
    assert view.state is TaskState.NEEDS_HUMAN
    assert [len(a.requests) for a in agents] == [1, 1, 1]
    for agent in agents:
        agent._scenario = FakeAgentScenario(output={"actions": [
            {"action": "report_progress", "recipient": {"kind": "role", "role": "orchestrator"}, "content": "Processed"},
            {"action": "finish_turn", "content": "One turn"},
        ]})
    yield runtime.service, view, agents
    await runtime.service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["planner", "implementer", "reviewer"])
async def test_first_wait_is_production_path_and_one_http_turn(production_paused, role):
    service, view, agents = production_paused
    room = await service.get_room(view.task_id)
    async with client(service) as api:
        posted = await api.post(f"/api/v1/tasks/{view.task_id}/messages", json={
            "expected_revision": view.revision, "idempotency_key": str(uuid4()),
            "content": "Keep compatibility", "recipient_role": role,
        })
        assert posted.status_code == 201, posted.text
        body = {"expected_revision": view.revision, "message_id": posted.json()["message"]["message_id"],
                "target_role": role, "idempotency_key": str(uuid4())}
        before = runtime_tests.state(service, view)
        accepted = await api.post(url(view), json=body)
        assert accepted.status_code == 202, accepted.text
        status = accepted.json()
        assert status["receipt"]["state"] == "claimed"  # Admission, not Agent completion.
        assert status["receipt"]["task_completion_evaluated"] is False
        assert "claim_token" not in accepted.text
        request_id = status["receipt"]["request"]["request_id"]
        await finished(service, request_id)
        queried = await api.get(f"/api/v1/tasks/{view.task_id}/continuations/{request_id}")
        assert queried.status_code == 200 and queried.json()["receipt"]["state"] == "succeeded"
        assert (await api.post(url(view), json=body)).json() == queried.json()
    index = ("planner", "implementer", "reviewer").index(role)
    assert [len(a.requests) for a in agents] == [2 if i == index else 1 for i in range(3)]
    turn = agents[index].requests[-1]
    assert turn.resume_from_session_id is None and turn.trace_id == view.trace_id
    assert turn.permission_mode is (PermissionMode.WORKSPACE_WRITE if role == "implementer" else PermissionMode.READ_ONLY)
    assert service.tasks.get(view.task_id) == before[0]
    assert service.contexts.get(view.task_id).revision == before[1].revision + 1
    assert service.rooms.get_message(UUID(body["message_id"])).deliveries[0].status.value == "acknowledged"
    assert not service.router.trace_store.list(trace_id=view.trace_id, type=TraceEventType.COMPLETION_DECIDED)
    assert len(service.router.trace_store.list(trace_id=view.trace_id, type=TraceEventType.CONTINUATION_EXECUTION_ACCEPTED)) == 1
    assert room.room.room_id == service.contexts.get(view.task_id).context.room_id


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["planner", "implementer", "reviewer"])
async def test_authorized_followup_consumes_resumes_and_parks_without_old_success(paused, role):
    service, view, agents = paused
    body = await granted(paused, role)
    before = runtime_tests.state(service, view)
    async with client(service) as api:
        accepted = await api.post(url(view), json=body)
        assert accepted.status_code == 202, accepted.text
        request_id = accepted.json()["receipt"]["request"]["request_id"]
        assert accepted.json()["task_state"] == {"planner": "planning", "implementer": "implementing", "reviewer": "verifying"}[role]
        record = await finished(service, request_id)
        assert record.receipt.state is ContinuationState.SUCCEEDED
        replay = await api.post(url(view), json=body)
        assert replay.status_code == 202 and replay.json()["receipt"]["state"] == "succeeded"
        changed = {**body, "authorization_id": None}
        assert (await api.post(url(view), json=changed)).status_code == 409
    index = ("planner", "implementer", "reviewer").index(role)
    assert [len(a.requests) for a in agents] == [2 + (i == index) if i == 0 else 1 + (i == index) for i in range(3)]
    snapshot = service.tasks.get(view.task_id)
    assert snapshot.task.state is TaskState.NEEDS_HUMAN and snapshot.revision == before[0].revision + 2
    assert snapshot.task.rework_rounds == before[0].task.rework_rounds
    assert service.contexts.get(view.task_id).revision == before[1].revision + 2
    assert not service.router.trace_store.list(trace_id=view.trace_id, type=TraceEventType.COMPLETION_DECIDED)
    assert not service._continuation_runs
    assert service.continuation_resumptions.get(task_id=view.task_id, authorization_id=UUID(body["authorization_id"])) is not None


@pytest.mark.asyncio
async def test_concurrent_duplicates_dispatch_once_and_changed_intent_conflicts(paused):
    service, view, agents = paused
    body = await command(paused)
    async with client(service) as api:
        responses = await asyncio.gather(*(api.post(url(view), json=body) for _ in range(5)))
        assert all(response.status_code == 202 for response in responses)
        ids = {r.json()["receipt"]["request"]["request_id"] for r in responses}
        assert len(ids) == 1
        await finished(service, ids.pop())
        for change in ({"target_role": "implementer"}, {"expected_revision": view.revision + 1}, {"message_id": str(uuid4())}):
            assert (await api.post(url(view), json={**body, **change})).status_code == 409
        another = await command(paused)
        assert (await api.post(url(view), json=another)).status_code == 409
    assert [len(a.requests) for a in agents] == [2, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
@pytest.mark.parametrize("fault", ["start", "exit", "malformed", "old_approval", "commit"])
async def test_failed_async_turn_persists_non_success_and_never_replays(paused, monkeypatch, authorized, fault):
    service, view, agents = paused
    role = "reviewer" if fault == "old_approval" else "planner"
    body = await granted(paused, role) if authorized else await command(paused, role)
    before = runtime_tests.state(service, view)
    scenario = agents[0]._scenario
    if fault == "start":
        agents[0]._scenario = replace(scenario, start_error="unavailable")
    elif fault == "exit":
        from app.agents import AgentExitReason
        agents[0]._scenario = replace(scenario, reason=AgentExitReason.FAILED, exit_code=1)
    elif fault == "malformed":
        agents[0]._scenario = replace(scenario, output={"not_actions": []})
    elif fault == "old_approval":
        scenario = agents[2]._scenario
        agents[2]._scenario = replace(scenario, output={"actions": [
            {"action": "approve_review", "recipient": {"kind": "role", "role": "orchestrator"},
             "content": "Old tests passed", "artifact_content": {"issues": []}},
            {"action": "finish_turn", "content": "Done"},
        ]})
    else:
        def fail(*args, **kwargs):
            raise sqlite3.OperationalError("injected commit error")
        monkeypatch.setattr(service.continuations, "finish", fail)
    async with client(service) as api:
        accepted = await api.post(url(view), json=body)
        assert accepted.status_code == 202, accepted.text
        request_id = accepted.json()["receipt"]["request"]["request_id"]
        record = await finished(service, request_id)
        assert record.receipt.state is ContinuationState.NEEDS_HUMAN
        assert record.receipt.failure_code == "execution_failed"
        count = [len(a.requests) for a in agents]
        replay = await api.post(url(view), json=body)
        assert replay.status_code == 202 and replay.json()["receipt"]["state"] == "needs_human"
        assert [len(a.requests) for a in agents] == count
    assert service.tasks.get(view.task_id).task.state is TaskState.NEEDS_HUMAN
    assert service.rooms.get_message(UUID(body["message_id"])).deliveries[0].status.value == "pending"
    assert not service.router.trace_store.list(trace_id=view.trace_id, type=TraceEventType.COMPLETION_DECIDED)
    assert service.contexts.get(view.task_id).revision == before[1].revision + int(authorized)


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
@pytest.mark.parametrize("during_start", [False, True])
async def test_query_and_cancel_do_not_wait_on_long_turn(paused, monkeypatch, authorized, during_start):
    service, view, agents = paused
    body = await granted(paused) if authorized else await command(paused)
    agents[0]._scenario = replace(agents[0]._scenario, block_until_cancel=True)
    entered = asyncio.Event()
    start = agents[0].start

    async def blocked(request):
        if during_start:
            entered.set()
            await asyncio.Event().wait()
        session = await start(request)
        entered.set()
        return session
    monkeypatch.setattr(agents[0], "start", blocked)
    async with client(service) as api:
        accepted = await asyncio.wait_for(api.post(url(view), json=body), timeout=1)
        assert accepted.status_code == 202
        request_id = accepted.json()["receipt"]["request"]["request_id"]
        endpoint = f"/api/v1/tasks/{view.task_id}/continuations/{request_id}"
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert not service._lock.locked()
        current = (await asyncio.wait_for(api.get(endpoint), timeout=1)).json()
        assert (await api.post(f"/api/v1/tasks/{view.task_id}/messages", json={
            "expected_revision": current["task_revision"], "idempotency_key": str(uuid4()),
            "recipient_role": "planner", "content": "Do not append during active execution",
        })).status_code == 409
        cancel = {"idempotency_key": str(uuid4()), "expected_revision": current["task_revision"],
                  "expected_runtime_revision": current["runtime_revision"], "expected_claim_updated_at": current["updated_at"],
                  "reason": "Stop owned turn"}
        ordinary_cancel = await api.post(f"/api/v1/tasks/{view.task_id}/cancel", json={"expected_revision": current["task_revision"]})
        assert ordinary_cancel.status_code == 409
        response = await asyncio.wait_for(api.post(endpoint + "/cancel", json=cancel), timeout=1)
        assert response.status_code == 202, response.text
        record = await finished(service, request_id)
        assert record.receipt.failure_code == "cancelled"
        observed = (await api.get(endpoint + "/cancellation")).json()
        assert observed["state"] == "observed" and observed["external_process_stopped_confirmed"] is False
        assert observed["observation"]["outcome"] == ("no_session" if during_start else "adapter_terminal_result")
        assert (await api.post(endpoint + "/cancel", json=cancel)).json() == observed
        assert (await api.post(url(view), json=body)).json()["receipt"]["state"] == "needs_human"
    assert service.tasks.get(view.task_id).task.state is TaskState.NEEDS_HUMAN
    assert service.rooms.get_message(UUID(body["message_id"])).deliveries[0].status.value == "pending"
    assert not service._continuation_runs


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
async def test_cancel_before_worker_entry_is_recorded_and_never_runs_adapter(paused, authorized):
    from app.api.models import CancelContinuationRequest, ContinueTaskRequest

    service, view, agents = paused
    body = await granted(paused) if authorized else await command(paused)
    before = [len(a.requests) for a in agents]
    accepted = await service.continue_task(view.task_id, ContinueTaskRequest(**body))
    claim = service.continuations.get(accepted.receipt.request.request_id)
    cancel = CancelContinuationRequest(idempotency_key=uuid4(), expected_revision=accepted.task_revision,
        expected_runtime_revision=accepted.runtime_revision, expected_claim_updated_at=claim.updated_at, reason="Pre-entry cancel")
    await service.cancel_continuation(view.task_id, claim.receipt.request.request_id, cancel)
    record = await finished(service, claim.receipt.request.request_id)
    assert record.receipt.failure_code == "cancelled"
    observed = service.continuation_cancellations.get(task_id=view.task_id, request_id=claim.receipt.request.request_id)
    assert observed.observation.outcome == "no_observation"
    assert [len(a.requests) for a in agents] == before
    assert service.tasks.get(view.task_id).task.state is TaskState.NEEDS_HUMAN


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("expected_revision", True), ("expected_revision", "1"), ("idempotency_key", "invalid"),
    ("target_role", "human"), ("authorization_id", "invalid"), ("content", "new issue"),
    ("claim_token", str(uuid4())), ("task_completion_evaluated", True), ("budget_reset", True),
])
async def test_strict_command_rejects_extra_authority_without_side_effects(paused, field, value):
    service, view, agents = paused
    body = await command(paused)
    before = runtime_tests.state(service, view)
    async with client(service) as api:
        assert (await api.post(url(view), json={**body, field: value})).status_code == 422
    assert runtime_tests.state(service, view) == before
    assert [len(a.requests) for a in agents] == [1, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["revision", "target", "missing_message", "wrong_task", "turn_budget", "message_budget", "registry", "worktree", "policy", "evidence"])
async def test_preparation_revalidates_scope_budget_permissions_before_admission(paused, fault):
    service, view, agents = paused
    body = await command(paused)
    endpoint = url(view)
    if fault == "revision":
        body["expected_revision"] += 1
    elif fault == "target":
        body["target_role"] = "implementer"
    elif fault == "missing_message":
        body["message_id"] = str(uuid4())
    elif fault == "wrong_task":
        endpoint = endpoint.replace(str(view.task_id), str(uuid4()))
    elif fault == "turn_budget":
        service.event_loop.executor.budget_guard.policy = ConversationBudgetPolicy(max_agent_turns=1)
    elif fault == "message_budget":
        guard = service.event_loop.executor.budget_guard
        guard.policy = ConversationBudgetPolicy(max_room_messages=guard.usage(view.task_id, room_id=service.contexts.get(view.task_id).context.room_id).room_messages + 1)
    elif fault == "registry":
        service.event_loop.executor.turns.registry.unregister(agents[0].name)
    elif fault == "worktree":
        service.worktrees._manifest_path(view.task_id).unlink()
    elif fault == "policy":
        service.verification_plan = service.verification_plan.model_copy(update={"commands": ()})
    else:
        plan = service.rooms.latest_plan_revision(service.contexts.get(view.task_id).context.room_id)
        service.router.artifacts.blob_path_for(plan.artifact_id).write_text("corrupt")
    before = runtime_tests.state(service, view)
    async with client(service) as api:
        assert (await api.post(endpoint, json=body)).status_code in {404, 409, 422}
    assert runtime_tests.state(service, view) == before
    assert service.continuations.active_for_task(view.task_id) is None
    assert [len(a.requests) for a in agents] == [1, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
async def test_http_admission_trace_failure_rolls_back_everything(paused, monkeypatch, authorized):
    service, view, agents = paused
    body = await granted(paused) if authorized else await command(paused)
    before = runtime_tests.state(service, view)
    before_calls = [len(a.requests) for a in agents]
    original = service.continuations.traces.append_in_transaction
    def fail(connection, event):
        if event.type is TraceEventType.CONTINUATION_EXECUTION_ACCEPTED:
            raise sqlite3.OperationalError("injected admission error")
        return original(connection, event)
    monkeypatch.setattr(service.continuations.traces, "append_in_transaction", fail)
    async with client(service) as api:
        response = await api.post(url(view), json=body)
        assert response.status_code == 503, response.text
    assert runtime_tests.state(service, view) == before
    assert [len(a.requests) for a in agents] == before_calls
    assert service.continuations.active_for_task(view.task_id) is None
    if authorized:
        assert service.continuation_resumptions.get(task_id=view.task_id, authorization_id=UUID(body["authorization_id"])) is None


@pytest.mark.asyncio
async def test_internal_staged_grant_is_not_adopted_by_http(paused):
    service, view, agents = paused
    kernel, grant = await resumption_tests.authorized(paused)
    await kernel.resume(view.task_id, grant.authorization_id)
    body = {"expected_revision": grant.intent.expected_revision, "message_id": str(grant.intent.message_id),
            "target_role": "planner", "idempotency_key": str(grant.intent.idempotency_key),
            "authorization_id": str(grant.authorization_id)}
    before = runtime_tests.state(service, view)
    async with client(service) as api:
        assert (await api.post(url(view), json=body)).status_code == 409
    assert runtime_tests.state(service, view) == before
    assert [len(a.requests) for a in agents] == [2, 1, 1]
    assert not service._continuation_runs


@pytest.mark.asyncio
async def test_internal_pending_is_not_adopted_or_dispatched(paused):
    from tests.test_continuation_claims import prepared_intent
    service, view, agents = paused
    _, request, _, intent = await prepared_intent(paused)
    service.continuations.register(intent)
    body = {**request.model_dump(mode="json"), "idempotency_key": str(intent.idempotency_key)}
    before = runtime_tests.state(service, view)
    async with client(service) as api:
        assert (await api.post(url(view), json=body)).status_code == 409
    assert runtime_tests.state(service, view) == before
    assert [len(a.requests) for a in agents] == [1, 1, 1]


def test_openapi_202_strict_schema_and_missing_capability():
    from types import SimpleNamespace
    body = {"expected_revision": 1, "message_id": str(uuid4()), "target_role": "planner", "idempotency_key": str(uuid4())}
    for service in (None, SimpleNamespace()):
        app = create_app(task_service=service)
        response = TestClient(app).post(f"/api/v1/tasks/{uuid4()}/continue", json=body)
        assert response.status_code == 503
    schema = app.openapi()
    assert "202" in schema["paths"]["/api/v1/tasks/{task_id}/continue"]["post"]["responses"]
    assert schema["components"]["schemas"]["ContinueTaskRequest"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_entire_http_first_then_authorized_turn_requires_no_manual_kernel(production_paused):
    service, view, agents = production_paused
    from tests.test_continuation_authorization import usage

    async with client(service) as api:
        first_body = await command(production_paused)
        first = await api.post(url(view), json=first_body)
        first_id = first.json()["receipt"]["request"]["request_id"]
        await finished(service, first_id)
        previous = (await api.get(f"/api/v1/tasks/{view.task_id}/continuations/{first_id}")).json()
        posted = await api.post(f"/api/v1/tasks/{view.task_id}/messages", json={
            "expected_revision": previous["task_revision"], "idempotency_key": str(uuid4()),
            "recipient_role": "implementer", "content": "Implement the additional boundary case",
        })
        assert posted.status_code == 201
        grant_command = {"expected_revision": previous["task_revision"], "expected_runtime_revision": previous["runtime_revision"],
                         "expected_claim_updated_at": previous["updated_at"], "idempotency_key": str(uuid4()),
                         "message_id": posted.json()["message"]["message_id"], "target_role": "implementer", "reason": "New requirement"}
        before_usage = usage(service, view)
        grant = await api.post(f"/api/v1/tasks/{view.task_id}/continuations/{first_id}/authorize", json=grant_command)
        assert grant.status_code == 200, grant.text
        body = {k: grant_command[k] for k in ("expected_revision", "idempotency_key", "message_id", "target_role")}
        body["authorization_id"] = grant.json()["authorization_id"]
        accepted = await api.post(url(view), json=body)
        assert accepted.status_code == 202, accepted.text
        record = await finished(service, accepted.json()["receipt"]["request"]["request_id"])
        assert record.receipt.state is ContinuationState.SUCCEEDED
        assert usage(service, view).agent_turns == before_usage.agent_turns + 1
    assert [len(a.requests) for a in agents] == [2, 2, 1]
    assert service.tasks.get(view.task_id).task.state is TaskState.NEEDS_HUMAN
    assert not service.router.trace_store.list(trace_id=view.trace_id, type=TraceEventType.COMPLETION_DECIDED)


@pytest.mark.asyncio
async def test_first_planner_question_before_any_plan_is_resumable_through_http(tmp_path):
    runtime, agents = make_runtime(tmp_path)
    service = runtime.service
    agents[0]._scenario = FakeAgentScenario(output={"actions": [
        {"action": "request_human_input", "recipient": {"kind": "role", "role": "human"}, "content": "Which behavior?"},
        {"action": "finish_turn", "content": "Wait"},
    ]})
    repository = make_repository(tmp_path)
    try:
        created = await service.create_task(CreateTaskRequest(issue="Clarify behavior", repository_path=str(repository)))
        await service.wait_for(created.task_id)
        view = await service.get_task(created.task_id)
        assert view.state is TaskState.NEEDS_HUMAN and [len(a.requests) for a in agents] == [1, 0, 0]
        room_id = service.contexts.get(view.task_id).context.room_id
        question = next(m for m in service.rooms.list_messages(room_id) if m.message.type.value == "human_input_request")
        agents[0]._scenario = FakeAgentScenario(output={"actions": [
            {"action": "share_plan", "recipient": {"kind": "role", "role": "implementer"}, "content": "Plan from answer",
             "artifact_content": {"steps": ["Change src/app.py"]}},
            {"action": "finish_turn", "content": "Plan done"},
        ]})
        async with client(service) as api:
            posted = await api.post(f"/api/v1/tasks/{view.task_id}/messages", json={
                "expected_revision": view.revision, "idempotency_key": str(uuid4()),
                "reply_to": str(question.message.message_id), "content": "Use behavior two",
            })
            assert posted.status_code == 201, posted.text
            body = {"expected_revision": view.revision, "message_id": posted.json()["message"]["message_id"],
                    "idempotency_key": str(uuid4()), "target_role": "planner"}
            accepted = await api.post(url(view), json=body)
            assert accepted.status_code == 202, accepted.text
            record = await finished(service, accepted.json()["receipt"]["request"]["request_id"])
            assert record.receipt.state is ContinuationState.SUCCEEDED
        assert [len(a.requests) for a in agents] == [2, 0, 0]  # Handoff does not auto-wake the teammate.
        assert service.rooms.latest_plan_revision(room_id).version == 1
        assert service.tasks.get(view.task_id).task.state is TaskState.NEEDS_HUMAN
    finally:
        await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
async def test_finish_transaction_failure_rolls_back_acks_runtime_and_success(paused, monkeypatch, authorized):
    service, view, _ = paused
    body = await granted(paused) if authorized else await command(paused)
    before = runtime_tests.state(service, view)
    original = service.continuations.traces.append_in_transaction
    def fail(connection, event):
        if event.type is TraceEventType.CONTINUATION_SUCCEEDED:
            raise sqlite3.OperationalError("injected after ACK and Runtime writes")
        return original(connection, event)
    monkeypatch.setattr(service.continuations.traces, "append_in_transaction", fail)
    async with client(service) as api:
        accepted = await api.post(url(view), json=body)
        record = await finished(service, accepted.json()["receipt"]["request"]["request_id"])
        assert record.receipt.failure_code == "execution_failed"
    assert service.contexts.get(view.task_id).revision == before[1].revision + int(authorized)
    assert service.rooms.get_message(UUID(body["message_id"])).deliveries[0].status.value == "pending"
    assert service.tasks.get(view.task_id).task.state is TaskState.NEEDS_HUMAN
    assert len(service.router.trace_store.list(trace_id=view.trace_id, type=TraceEventType.CONTINUATION_SUCCEEDED)) == len([
        entry for entry in before[3] if entry.event.type is TraceEventType.CONTINUATION_SUCCEEDED
    ])


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
async def test_budget_changed_after_admission_stops_without_agent_call(paused, authorized):
    from app.api.models import ContinueTaskRequest
    service, view, agents = paused
    body = await granted(paused) if authorized else await command(paused)
    before = [len(a.requests) for a in agents]
    accepted = await service.continue_task(view.task_id, ContinueTaskRequest(**body))
    guard = service.event_loop.executor.budget_guard
    usage = guard.usage(view.task_id, room_id=service.contexts.get(view.task_id).context.room_id)
    guard.policy = ConversationBudgetPolicy(max_agent_turns=usage.agent_turns)
    record = await finished(service, accepted.receipt.request.request_id)
    assert record.receipt.failure_code == "budget_blocked"
    assert [len(a.requests) for a in agents] == before
    assert service.rooms.get_message(UUID(body["message_id"])).deliveries[0].status.value == "pending"
    assert service.tasks.get(view.task_id).task.state is TaskState.NEEDS_HUMAN


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
async def test_shutdown_before_entry_parks_claim_without_retry_or_fake_stop_proof(paused, authorized):
    from app.api.models import ContinueTaskRequest
    service, view, agents = paused
    body = await granted(paused) if authorized else await command(paused)
    before = [len(a.requests) for a in agents]
    accepted = await service.continue_task(view.task_id, ContinueTaskRequest(**body))
    await service.shutdown()
    record = service.continuations.get(accepted.receipt.request.request_id)
    assert record.receipt.failure_code == "cancelled"
    assert service.tasks.get(view.task_id).task.state is TaskState.NEEDS_HUMAN
    assert not service._continuation_runs and [len(a.requests) for a in agents] == before
    async with client(service) as api:
        assert (await api.post(url(view), json=body)).json()["receipt"]["state"] == "needs_human"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["json", "fingerprint", "index"])
async def test_corrupt_http_admission_never_dispatches_again(paused, fault):
    service, view, agents = paused
    body = await command(paused)
    async with client(service) as api:
        accepted = await api.post(url(view), json=body)
        await finished(service, accepted.json()["receipt"]["request"]["request_id"])
        with service.tasks.database.transaction() as connection:
            column, value = {"json": ("event_json", "{}"), "fingerprint": ("event_fingerprint", "0" * 64),
                             "index": ("event_type", "continuation_resumed")}[fault]
            connection.execute(f"UPDATE trace_events SET {column}=? WHERE event_type=?", (value, TraceEventType.CONTINUATION_EXECUTION_ACCEPTED.value))
        assert (await api.post(url(view), json=body)).status_code == 503
    assert [len(a.requests) for a in agents] == [2, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
async def test_other_service_can_query_and_replay_but_cannot_adopt_owner(paused, monkeypatch, authorized):
    from app.api.persistent_service import PersistentTaskService
    service, view, agents = paused
    body = await granted(paused) if authorized else await command(paused)
    agents[0]._scenario = replace(agents[0]._scenario, block_until_cancel=True)
    entered = asyncio.Event()
    start = agents[0].start
    async def blocked(request):
        session = await start(request)
        entered.set()
        return session
    monkeypatch.setattr(agents[0], "start", blocked)
    try:
        async with client(service) as api:
            accepted = await api.post(url(view), json=body)
            assert accepted.status_code == 202
        await asyncio.wait_for(entered.wait(), timeout=2)
        other = PersistentTaskService(tasks=service.tasks, contexts=service.contexts, rooms=service.rooms, router=service.router,
            worktrees=service.worktrees, event_loop=service.event_loop, verification_plan=service.verification_plan,
            agent_names=service.agent_names)
        before = [len(a.requests) for a in agents]
        async with client(other) as api:
            replay = await api.post(url(view), json=body)
            assert replay.status_code == 202 and replay.json()["receipt"]["state"] == "claimed"
            request_id = replay.json()["receipt"]["request"]["request_id"]
            query = await api.get(f"/api/v1/tasks/{view.task_id}/continuations/{request_id}")
            assert query.json() == replay.json()
            cancel = {"idempotency_key": str(uuid4()), "expected_revision": replay.json()["task_revision"],
                      "expected_runtime_revision": replay.json()["runtime_revision"], "expected_claim_updated_at": replay.json()["updated_at"], "reason": "No owner"}
            assert (await api.post(f"/api/v1/tasks/{view.task_id}/continuations/{request_id}/cancel", json=cancel)).status_code == 409
            assert not other._continuation_runs and [len(a.requests) for a in agents] == before
    finally:
        await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
async def test_shutdown_during_preparation_closes_admission_without_claim_or_dispatch(paused, monkeypatch, authorized):
    service, view, agents = paused
    body = await granted(paused) if authorized else await command(paused)
    before = runtime_tests.state(service, view)
    before_calls = [len(a.requests) for a in agents]
    inspected, release = asyncio.Event(), asyncio.Event()
    original = service.worktrees.inspect
    async def blocked(task_id):
        result = await original(task_id)
        inspected.set()
        await release.wait()
        return result
    monkeypatch.setattr(service.worktrees, "inspect", blocked)
    async with client(service) as api:
        response_task = asyncio.create_task(api.post(url(view), json=body))
        await asyncio.wait_for(inspected.wait(), timeout=2)
        shutdown_task = asyncio.create_task(service.shutdown())
        await asyncio.sleep(0)
        assert service._continuation_accepting is False
        release.set()
        response = await asyncio.wait_for(response_task, timeout=2)
        await asyncio.wait_for(shutdown_task, timeout=2)
        assert response.status_code == 503, response.text
        assert (await api.post(url(view), json=body)).status_code == 503
    assert runtime_tests.state(service, view) == before
    assert service.continuations.active_for_task(view.task_id) is None and not service._continuation_runs
    assert [len(a.requests) for a in agents] == before_calls
    if authorized:
        assert service.continuation_resumptions.get(task_id=view.task_id, authorization_id=UUID(body["authorization_id"])) is None
