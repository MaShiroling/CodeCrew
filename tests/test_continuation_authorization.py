"""New intent audit only: no release, dispatch, ACK or budget reset."""

import asyncio
import json
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.models import AuthorizeContinuationRequest
from app.main import create_app
from app.storage import SQLiteDatabase
from app.storage.continuation_authorizations import ContinuationAuthorizationRepository
from app.storage.continuations import ContinuationConflictError
from app.team import MemberRole
from app.team.budgets import ConversationBudgetPolicy
from app.trace import TraceEventType
from tests import test_continuation_runtime as runtime_tests
from tests.test_continuation_claims import prepared_intent

paused = runtime_tests.paused


async def setup(paused, role="planner"):
    service, view, _ = paused
    kernel, old_request = await runtime_tests.intent(paused)
    result = await kernel.run_single(view.task_id, old_request)
    previous = service.continuations.get(result.receipt.request.request_id)
    _, new_request = await runtime_tests.intent(paused, recipient=role)
    body = {
        "idempotency_key": str(uuid4()), "message_id": str(new_request.message_id),
        "target_role": role, "expected_revision": view.revision,
        "expected_runtime_revision": service.contexts.get(view.task_id).revision,
        "expected_claim_updated_at": previous.updated_at.isoformat(),
        "reason": "Review the new Human requirement, without reusing prior success.",
    }
    url = f"/api/v1/tasks/{view.task_id}/continuations/{previous.receipt.request.request_id}/authorize"
    return previous, body, url


def usage(service, view):
    return service.event_loop.executor.budget_guard.usage(
        view.task_id, room_id=service.contexts.get(view.task_id).context.room_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["planner", "implementer", "reviewer"])
async def test_authorization_is_bound_durable_idempotent_and_does_not_execute(paused, role):
    service, view, agents = paused
    previous, body, url = await setup(paused, role)
    before = runtime_tests.state(service, view)
    before_usage = usage(service, view)
    async with AsyncClient(transport=ASGITransport(app=create_app(task_service=service)), base_url="http://test") as client:
        response = await client.post(url, json=body)
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["intent"]["message_id"] == body["message_id"]
        assert receipt["intent"]["target_role"] == role
        assert receipt["previous_request_id"] == str(previous.receipt.request.request_id)
        assert receipt["artifacts"]
        for field in ("execution_ready", "agent_dispatched", "claim_released", "budget_reset",
                      "task_completion_evaluated", "external_process_stopped_confirmed"):
            assert receipt[field] is False
        assert "claim_token" not in response.text
        assert (await client.post(url, json=body)).json() == receipt
        endpoint = f"/api/v1/tasks/{view.task_id}/continuation-authorizations/{receipt['authorization_id']}"
        assert (await client.get(endpoint)).json() == receipt
    assert runtime_tests.state(service, view)[:3] == before[:3]
    assert usage(service, view) == before_usage
    assert [len(agent.requests) for agent in agents] == [2, 1, 1]
    assert service.continuations.active_for_task(view.task_id) is None
    assert service.continuations.get(previous.receipt.request.request_id) == previous
    events = service.router.trace_store.list(trace_id=view.trace_id, type=TraceEventType.CONTINUATION_AUTHORIZED)
    assert len(events) == 1 and str(events[0].event.causation_id) == body["message_id"]
    reopened = ContinuationAuthorizationRepository(service.continuations)
    reopened.database = SQLiteDatabase(service.tasks.database.path)
    reopened.initialize()
    assert reopened.database.schema_version == 14
    from uuid import UUID
    assert reopened.get(task_id=view.task_id, authorization_id=UUID(receipt["authorization_id"])).model_dump(mode="json") == receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "claimed", "cancelled", "failed", "quarantined"])
async def test_unresolved_claim_never_becomes_new_authority(paused, state):
    service, view, agents = paused
    _, _, _, intent = await prepared_intent(paused)
    old = service.continuations.register(intent)
    if state != "pending":
        old = service.continuations.claim(intent.request_id)
    if state in {"cancelled", "failed"}:
        service.continuations.pause(old, code="cancelled" if state == "cancelled" else "execution_failed")
        old = service.continuations.get(intent.request_id)
    if state == "quarantined":
        from app.storage.continuations import QuarantineContinuationCommand
        room = service.rooms.get_room(intent.room_id)
        human = next(m for m in room.members if m.role is MemberRole.HUMAN)
        service.continuations.quarantine(task_id=view.task_id, request_id=intent.request_id,
            human_member_id=human.member_id, command=QuarantineContinuationCommand(
                idempotency_key=uuid4(), expected_revision=view.revision,
                expected_runtime_revision=service.contexts.get(view.task_id).revision,
                expected_claim_updated_at=old.updated_at, reason="Unknown execution stays fenced"))
    _, request = await runtime_tests.intent(paused)
    body = {"idempotency_key": str(uuid4()), "message_id": str(request.message_id), "target_role": "planner",
            "expected_revision": view.revision, "expected_runtime_revision": service.contexts.get(view.task_id).revision,
            "expected_claim_updated_at": old.updated_at.isoformat(), "reason": "New intent"}
    before = runtime_tests.state(service, view)
    async with AsyncClient(transport=ASGITransport(app=create_app(task_service=service)), base_url="http://test") as client:
        response = await client.post(f"/api/v1/tasks/{view.task_id}/continuations/{intent.request_id}/authorize", json=body)
        assert response.status_code == 409, response.text
    assert service.continuations.get(intent.request_id) == old
    assert runtime_tests.state(service, view) == before
    assert [len(agent.requests) for agent in agents] == [1, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("expected_revision", True), ("expected_runtime_revision", "3"),
    ("expected_claim_updated_at", "2026-09-28T00:00:00"), ("reason", " "),
    ("target_role", "human"), ("claim_released", True), ("pid", 1),
    ("human_member_id", str(uuid4())), ("budget_reset", True),
])
async def test_invalid_or_expanded_authority_rejected(paused, field, value):
    service, view, _ = paused
    _, body, url = await setup(paused)
    before = runtime_tests.state(service, view)
    body[field] = value
    async with AsyncClient(transport=ASGITransport(app=create_app(task_service=service)), base_url="http://test") as client:
        assert (await client.post(url, json=body)).status_code == 422
    assert runtime_tests.state(service, view) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["revision", "runtime", "timestamp", "old_message", "wrong_target", "budget", "cross_task"])
async def test_stale_scope_source_and_budget_fail_closed(paused, fault):
    service, view, _ = paused
    previous, body, url = await setup(paused)
    if fault == "revision":
        body["expected_revision"] += 1
    elif fault == "runtime":
        body["expected_runtime_revision"] += 1
    elif fault == "timestamp":
        body["expected_claim_updated_at"] = "2000-01-01T00:00:00Z"
    elif fault == "old_message":
        body["message_id"] = str(previous.receipt.request.message_id)
    elif fault == "wrong_target":
        body["target_role"] = "implementer"
    elif fault == "budget":
        service.event_loop.executor.budget_guard.policy = ConversationBudgetPolicy(max_agent_turns=1)
    else:
        url = url.replace(str(view.task_id), str(uuid4()))
    before = runtime_tests.state(service, view)
    async with AsyncClient(transport=ASGITransport(app=create_app(task_service=service)), base_url="http://test") as client:
        assert (await client.post(url, json=body)).status_code in {404, 409, 422}
    assert runtime_tests.state(service, view) == before


@pytest.mark.asyncio
async def test_changed_command_conflicts_and_historical_replay_does_not_renew(paused):
    service, view, _ = paused
    _, body, url = await setup(paused)
    async with AsyncClient(transport=ASGITransport(app=create_app(task_service=service)), base_url="http://test") as client:
        first = await client.post(url, json=body)
        assert first.status_code == 200
        for change in ({"reason": "different"}, {"target_role": "reviewer"}, {"idempotency_key": str(uuid4())}):
            assert (await client.post(url, json={**body, **change})).status_code == 409
        context = service.contexts.get(view.task_id)
        service.contexts.save(context.context, expected_revision=context.revision)
        assert (await client.post(url, json=body)).json() == first.json()
        assert first.json()["execution_ready"] is False


@pytest.mark.asyncio
async def test_trace_failure_rolls_back_authorization(paused, monkeypatch):
    service, view, _ = paused
    previous, body, _ = await setup(paused)
    def fail(*args, **kwargs):
        import sqlite3
        raise sqlite3.OperationalError("injected trace failure")
    monkeypatch.setattr(service.continuations.traces, "append_in_transaction", fail)
    from app.api.service import TaskServiceUnavailable
    with pytest.raises(TaskServiceUnavailable):
        await service.authorize_continuation(view.task_id, previous.receipt.request.request_id, AuthorizeContinuationRequest(**body))
    with service.tasks.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM continuation_authorizations").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_cancelled_terminal_observation_is_not_a_release_permit(paused, monkeypatch):
    from tests import test_continuation_cancellation as cancellations
    service, view, _ = paused
    _, _, operation, claim = await cancellations.active(paused, monkeypatch)
    async with cancellations.client(service) as api:
        assert (await api.post(cancellations.url(view, claim) + "/cancel",
                               json=cancellations.body(service, view, claim))).status_code == 202
        with pytest.raises(asyncio.CancelledError):
            await operation
        observed = (await api.get(cancellations.url(view, claim) + "/cancellation")).json()
        assert observed["observation"]["outcome"] == "adapter_terminal_result"
        _, request = await runtime_tests.intent(paused)
        paused_claim = service.continuations.get(claim.receipt.request.request_id)
        response = await api.post(cancellations.url(view, claim) + "/authorize", json={
            "idempotency_key": str(uuid4()), "message_id": str(request.message_id), "target_role": "planner",
            "expected_revision": view.revision, "expected_runtime_revision": service.contexts.get(view.task_id).revision,
            "expected_claim_updated_at": paused_claim.updated_at.isoformat(), "reason": "Explicit new intent",
        })
        assert response.status_code == 409
    assert service.continuations.active_for_task(view.task_id) == paused_claim


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["plan_blob", "worktree", "registry", "message_budget", "old_key"])
async def test_preparation_and_reservation_faults_do_not_write_grants(paused, fault):
    service, view, agents = paused
    previous, body, _ = await setup(paused)
    if fault == "plan_blob":
        plan = service.rooms.latest_plan_revision(service.contexts.get(view.task_id).context.room_id)
        service.router.artifacts.blob_path_for(plan.artifact_id).write_text("tampered")
    elif fault == "worktree":
        service.worktrees._manifest_path(view.task_id).unlink()
    elif fault == "registry":
        service.event_loop.executor.turns.registry.unregister(agents[0].name)
    elif fault == "message_budget":
        guard = service.event_loop.executor.budget_guard
        guard.policy = ConversationBudgetPolicy(max_room_messages=usage(service, view).room_messages + 1)
    else:
        body["idempotency_key"] = str(previous.receipt.request.idempotency_key)
    before = runtime_tests.state(service, view)
    from app.agents.registry import AgentRegistryError
    from app.api.service import TaskStateConflict
    from app.storage import ArtifactIntegrityError
    from app.workspace import WorktreeError
    with pytest.raises((ArtifactIntegrityError, WorktreeError, AgentRegistryError, TaskStateConflict)):
        await service.authorize_continuation(view.task_id, previous.receipt.request.request_id, AuthorizeContinuationRequest(**body))
    assert runtime_tests.state(service, view) == before
    assert [len(agent.requests) for agent in agents] == [2, 1, 1]


@pytest.mark.asyncio
async def test_source_mutation_is_not_renewed_by_historical_replay(paused):
    service, _, _ = paused
    _, body, url = await setup(paused)
    async with AsyncClient(transport=ASGITransport(app=create_app(task_service=service)), base_url="http://test") as client:
        assert (await client.post(url, json=body)).status_code == 200
        with service.tasks.database.transaction() as connection:
            row = connection.execute("SELECT message_json FROM chat_messages WHERE message_id=?", (body["message_id"],)).fetchone()
            content = json.loads(row["message_json"])
            content["content"] = "altered intent"
            connection.execute("UPDATE chat_messages SET message_json=? WHERE message_id=?", (json.dumps(content), body["message_id"]))
        assert (await client.post(url, json=body)).status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["json", "index", "predecessor"])
async def test_corruption_and_cross_scope_queries_are_rejected(paused, fault):
    service, view, _ = paused
    _, body, url = await setup(paused)
    async with AsyncClient(transport=ASGITransport(app=create_app(task_service=service)), base_url="http://test") as client:
        receipt = (await client.post(url, json=body)).json()
        endpoint = f"/api/v1/tasks/{view.task_id}/continuation-authorizations/{receipt['authorization_id']}"
        assert (await client.get(endpoint.replace(str(view.task_id), str(uuid4())))).status_code == 404
        with service.tasks.database.transaction() as connection:
            if fault == "json":
                connection.execute("UPDATE continuation_authorizations SET receipt_json='{}'")
            elif fault == "index":
                connection.execute("UPDATE continuation_authorizations SET runtime_revision=1000")
            else:
                changed = {**receipt, "previous_record_sha256": "0" * 64}
                from app.storage.continuation_authorizations import ContinuationAuthorizationReceipt
                connection.execute("UPDATE continuation_authorizations SET receipt_json=?", (
                    ContinuationAuthorizationReceipt(**changed).model_dump_json(),))
        assert (await client.get(endpoint)).status_code == 503
        assert (await client.post(url, json=body)).status_code == 503


@pytest.mark.asyncio
async def test_independent_connections_choose_one_new_intent(paused):
    service, view, _ = paused
    previous, body, _ = await setup(paused)
    first = await service.authorize_continuation(view.task_id, previous.receipt.request.request_id, AuthorizeContinuationRequest(**body))
    # Roll back only the authorization for a controlled independent-connection race.
    with service.tasks.database.transaction() as connection:
        connection.execute("DELETE FROM continuation_authorizations")
        connection.execute("DELETE FROM trace_events WHERE event_type=?", (TraceEventType.CONTINUATION_AUTHORIZED.value,))
    repositories = [ContinuationAuthorizationRepository(service.continuations) for _ in range(2)]
    for repo in repositories:
        repo.database = SQLiteDatabase(service.tasks.database.path)
    commands = [first.command, first.command.model_copy(update={"idempotency_key": uuid4(), "reason": "other decision"})]
    async def call(repo, command):
        intent = first.intent.model_copy(update={"idempotency_key": command.idempotency_key})
        try:
            return await asyncio.to_thread(repo.authorize, previous_request_id=first.previous_request_id,
                human_member_id=first.human_member_id, command=command, intent=intent,
                runtime_sha256=first.runtime_sha256, artifacts=first.artifacts)
        except ContinuationConflictError:
            return None
    results = await asyncio.gather(*(call(r, c) for r, c in zip(repositories, commands)))
    assert sum(r is not None for r in results) == 1


def test_unconfigured_api_and_openapi_contract():
    app = create_app()
    schema = app.openapi()
    properties = schema["components"]["schemas"]["AuthorizeContinuationRequest"]
    assert properties["additionalProperties"] is False
    assert "pid" not in properties["properties"]
    assert "202" in schema["paths"]["/api/v1/tasks/{task_id}/continue"]["post"]["responses"]
    from fastapi.testclient import TestClient
    response = TestClient(app).post(f"/api/v1/tasks/{uuid4()}/continuations/{uuid4()}/authorize", json={
        "idempotency_key": str(uuid4()), "message_id": str(uuid4()), "target_role": "planner",
        "expected_revision": 1, "expected_runtime_revision": 1,
        "expected_claim_updated_at": "2026-09-28T00:00:00Z", "reason": "new intent",
    })
    assert response.status_code == 503


def test_migration_13_is_additive_and_idempotent(tmp_path):
    from app.storage.continuation_cancellations import ContinuationCancellationRepository
    from app.storage.continuations import ContinuationRepository
    claims = ContinuationRepository(SQLiteDatabase(tmp_path / "migration.sqlite3"))
    claims.initialize()
    ContinuationCancellationRepository(claims).initialize()
    assert claims.database.schema_version == 12
    with claims.database.connect() as connection:
        before = [tuple(row) for row in connection.execute(
            "SELECT version,name,checksum,applied_at FROM schema_migrations ORDER BY version")]
    repository = ContinuationAuthorizationRepository(claims)
    repository.initialize()
    repository.initialize()
    assert claims.database.schema_version == 13
    with claims.database.connect() as connection:
        after = [tuple(row) for row in connection.execute(
            "SELECT version,name,checksum,applied_at FROM schema_migrations WHERE version<13 ORDER BY version")]
    assert after == before
