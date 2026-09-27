"""Human containment of uncertain claims: SQLite/HTTP/Fake, no model calls."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.models import QuarantineContinuationRequest
from app.api.service import TaskStateConflict
from app.main import create_app
from app.orchestration.models import Task, TaskState, utc_now
from app.storage import SQLiteDatabase
from app.storage.continuations import (
    CONTINUATION_MIGRATIONS,
    ContinuationConflictError,
    ContinuationRepository,
    ContinuationState,
)
from app.team import MemberRole
from app.trace import TraceActorKind, TraceEventType
from tests import test_continuation_runtime as runtime_tests
from tests.test_continuation_claims import prepared_intent

paused = runtime_tests.paused


async def claim_fixture(paused, *, state="claimed"):
    service, _, _ = paused
    kernel, request, prepared, intent = await prepared_intent(paused)
    service.continuations.register(intent)
    claim = service.continuations.claim(intent.request_id)
    if state == "needs_human":
        service.continuations.pause(claim, code="execution_failed")
        claim = service.continuations.get(intent.request_id)
    return kernel, request, prepared, claim


def body(paused, claim, **updates):
    service, view, _ = paused
    return {"idempotency_key": str(uuid4()), "expected_revision": view.revision,
            "expected_runtime_revision": service.contexts.get(view.task_id).revision,
            "expected_claim_updated_at": claim.updated_at.isoformat(),
            "reason": "Old execution outcome is uncertain; keep this task isolated.", **updates}


def url(view, claim):
    return f"/api/v1/tasks/{view.task_id}/continuations/{claim.receipt.request.request_id}"


def usage(paused):
    service, view, _ = paused
    return service.event_loop.executor.budget_guard.usage(
        view.task_id, room_id=service.contexts.get(view.task_id).context.room_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["claimed", "needs_human"])
async def test_human_quarantine_is_durable_idempotent_without_releasing_or_dispatch(paused, state):
    service, view, agents = paused
    kernel, request, _, claim = await claim_fixture(paused, state=state)
    client = TestClient(create_app(task_service=service))
    endpoint = url(view, claim)
    before = runtime_tests.state(service, view)
    before_usage = usage(paused)
    status = client.get(endpoint)
    assert status.status_code == 200
    assert status.json()["quarantine"] is None
    assert status.json()["runtime_revision"] == service.contexts.get(view.task_id).revision
    assert "claim_token" not in status.text and str(claim.claim_token) not in status.text
    assert runtime_tests.state(service, view) == before
    command = body(paused, claim)
    response = client.post(endpoint + "/quarantine", json=command)
    assert response.status_code == 200
    receipt = response.json()
    for field in ("claim_released", "external_process_stopped_confirmed", "agent_dispatched",
                  "budget_reset", "task_completion_evaluated"):
        assert receipt[field] is False
    human = next(m for m in service.rooms.get_room(claim.receipt.request.room_id).members
                 if m.role is MemberRole.HUMAN)
    assert receipt["human_member_id"] == str(human.member_id)
    assert receipt["observed_state"] == state
    assert client.post(endpoint + "/quarantine", json=command).json() == receipt
    assert client.get(endpoint).json()["quarantine"] == receipt
    assert "claim_token" not in response.text
    assert service.tasks.get(view.task_id) == before[0]
    assert service.contexts.get(view.task_id) == before[1]
    assert service.rooms.list_messages(claim.receipt.request.room_id) == before[2]
    assert usage(paused) == before_usage
    assert service.continuations.active_for_task(view.task_id) == claim
    reopened = ContinuationRepository(SQLiteDatabase(service.tasks.database.path))
    reopened.initialize()
    assert reopened.database.schema_version == 12
    assert reopened.status(task_id=view.task_id, request_id=claim.receipt.request.request_id).quarantine.model_dump(mode="json") == receipt
    replay = await kernel.run_single(view.task_id, request)
    assert replay.replayed and replay.receipt == claim.receipt
    assert [len(a.requests) for a in agents] == [1, 1, 1]
    with pytest.raises(TaskStateConflict):
        await kernel.prepare(view.task_id, request)
    _, different = await runtime_tests.intent(paused, recipient="implementer")
    with pytest.raises(ContinuationConflictError, match="reservation"):
        await kernel.run_single(view.task_id, different)
    events = service.router.trace_store.list(trace_id=view.trace_id, type=TraceEventType.CONTINUATION_QUARANTINED)
    assert len(events) == 1
    event = events[0].event
    assert event.actor_kind is TraceActorKind.HUMAN and event.actor_id == str(human.member_id)
    assert event.correlation_id == claim.receipt.request.correlation_id
    assert event.causation_id == request.message_id
    assert event.payload["claim_released"] is False


@pytest.mark.asyncio
async def test_quarantine_fences_late_owner_finish_and_pause_before_any_commit(paused):
    service, view, _ = paused
    _, _, prepared, claim = await claim_fixture(paused)
    command = QuarantineContinuationRequest(**body(paused, claim))
    await service.quarantine_continuation(view.task_id, claim.receipt.request.request_id, command)
    before = runtime_tests.state(service, view)
    # Deliberately invalid session/input: quarantine must reject before those
    # or any ACK/Runtime update are inspected, even with the true claim token.
    with pytest.raises(ContinuationConflictError, match="quarantined"):
        service.continuations.finish(claim, context=prepared.runtime.to_context(), session=None,
                                     input_ids=(), output_ids=())
    with pytest.raises(ContinuationConflictError, match="quarantined"):
        service.continuations.pause(claim, code="cancelled")
    assert runtime_tests.state(service, view) == before
    assert service.continuations.claim(claim.receipt.request.request_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("expected_revision", True), ("expected_revision", "1"),
    ("expected_runtime_revision", False), ("expected_runtime_revision", "1"),
    ("expected_claim_updated_at", "not a timestamp"), ("reason", " "), ("reason", "x" * 1001),
    ("disposition", "retry"), ("human_member_id", str(uuid4())),
    ("external_process_stopped_confirmed", True), ("claim_released", True), ("budget_reset", True),
])
async def test_invalid_or_authority_expanding_requests_are_rejected_without_writes(paused, field, value):
    service, view, _ = paused
    _, _, _, claim = await claim_fixture(paused)
    before = runtime_tests.state(service, view)
    client = TestClient(create_app(task_service=service))
    response = client.post(url(view, claim) + "/quarantine", json=body(paused, claim, **{field: value}))
    assert response.status_code == 422
    assert runtime_tests.state(service, view) == before
    assert client.get(url(view, claim)).json()["quarantine"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["task_revision", "runtime_revision", "claim_timestamp", "room", "human", "task_state"])
async def test_stale_or_invalid_scope_rejects_quarantine(paused, fault):
    service, view, _ = paused
    _, _, _, claim = await claim_fixture(paused)
    command = body(paused, claim)
    if fault == "task_revision":
        command["expected_revision"] += 1
    elif fault == "runtime_revision":
        command["expected_runtime_revision"] += 1
    elif fault == "claim_timestamp":
        command["expected_claim_updated_at"] = (claim.updated_at + timedelta(seconds=1)).isoformat()
    else:
        with service.tasks.database.transaction() as connection:
            if fault == "room":
                connection.execute("UPDATE team_rooms SET status='closed', closed_at=? WHERE room_id=?",
                                   (utc_now().isoformat(), str(claim.receipt.request.room_id)))
            elif fault == "human":
                connection.execute("UPDATE room_members SET kind='system' WHERE room_id=? AND role='human'",
                                   (str(claim.receipt.request.room_id),))
            else:
                snapshot = service.tasks.get(view.task_id)
                changed = snapshot.task.model_copy(update={"state": TaskState.FAILED})
                connection.execute("UPDATE tasks SET state='failed', task_json=? WHERE task_id=?",
                                   (changed.model_dump_json(), str(view.task_id)))
    client = TestClient(create_app(task_service=service))
    before = runtime_tests.state(service, view)
    response = client.post(url(view, claim) + "/quarantine", json=command)
    assert response.status_code == (503 if fault == "human" else 409)
    assert runtime_tests.state(service, view) == before
    assert not service.router.trace_store.list(trace_id=view.trace_id, type=TraceEventType.CONTINUATION_QUARANTINED)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["reason", "revision", "key"])
async def test_quarantine_cannot_be_overwritten_by_changed_intent(paused, field):
    service, view, _ = paused
    _, _, _, claim = await claim_fixture(paused)
    client = TestClient(create_app(task_service=service))
    endpoint = url(view, claim) + "/quarantine"
    command = body(paused, claim)
    first = client.post(endpoint, json=command).json()
    if field == "reason":
        command["reason"] = "Different decision"
    elif field == "revision":
        command["expected_revision"] += 1
    else:
        command["idempotency_key"] = str(uuid4())
    assert client.post(endpoint, json=command).status_code == 409
    assert client.get(url(view, claim)).json()["quarantine"] == first


@pytest.mark.asyncio
async def test_trace_write_failure_rolls_back_quarantine_and_fence(paused, monkeypatch):
    service, view, _ = paused
    _, _, _, claim = await claim_fixture(paused)
    repository = service.continuations
    before = runtime_tests.state(service, view)

    def fail(*args):
        raise RuntimeError("trace unavailable")

    monkeypatch.setattr(repository.traces, "append_in_transaction", fail)
    command = QuarantineContinuationRequest(**body(paused, claim))
    human = next(m for m in service.rooms.get_room(claim.receipt.request.room_id).members if m.role is MemberRole.HUMAN)
    with pytest.raises(RuntimeError, match="trace unavailable"):
        repository.quarantine(task_id=view.task_id, request_id=claim.receipt.request.request_id,
                              human_member_id=human.member_id, command=command)
    assert repository.status(task_id=view.task_id, request_id=claim.receipt.request.request_id).quarantine is None
    assert runtime_tests.state(service, view) == before
    # No audit means no containment was accepted. Existing owner can still pause.
    monkeypatch.undo()
    assert repository.pause(claim, code="execution_failed").state is ContinuationState.NEEDS_HUMAN


@pytest.mark.asyncio
async def test_independent_connections_race_to_one_quarantine(paused):
    service, view, _ = paused
    _, _, _, claim = await claim_fixture(paused)
    human = next(m for m in service.rooms.get_room(claim.receipt.request.room_id).members if m.role is MemberRole.HUMAN)
    command = QuarantineContinuationRequest(**body(paused, claim))
    repositories = [ContinuationRepository(SQLiteDatabase(service.tasks.database.path)) for _ in range(4)]
    receipts = await asyncio.gather(*(asyncio.to_thread(
        repo.quarantine, task_id=view.task_id, request_id=claim.receipt.request.request_id,
        human_member_id=human.member_id, command=command,
    ) for repo in repositories))
    assert len({receipt.resolution_id for receipt in receipts}) == 1
    assert len(service.router.trace_store.list(trace_id=view.trace_id, type=TraceEventType.CONTINUATION_QUARANTINED)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["index", "json", "claim", "binding"])
async def test_corrupt_quarantine_fails_closed_for_inspection_and_late_commit(paused, fault):
    service, view, _ = paused
    _, _, _, claim = await claim_fixture(paused)
    client = TestClient(create_app(task_service=service))
    endpoint = url(view, claim)
    assert client.post(endpoint + "/quarantine", json=body(paused, claim)).status_code == 200
    with service.tasks.database.transaction() as connection:
        if fault == "index":
            connection.execute("UPDATE continuation_quarantines SET trace_id=?", (str(uuid4()),))
        elif fault == "json":
            connection.execute("UPDATE continuation_quarantines SET receipt_json='{}'")
        elif fault == "claim":
            connection.execute("UPDATE continuation_requests SET updated_at='corrupt'")
        else:
            receipt = service.continuations.status(task_id=view.task_id, request_id=claim.receipt.request.request_id).quarantine
            changed = receipt.model_copy(update={"claim_record_sha256": "0" * 64})
            connection.execute("UPDATE continuation_quarantines SET receipt_json=?", (changed.model_dump_json(),))
    response = client.get(endpoint)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "task_service_unavailable"
    with pytest.raises(RuntimeError):
        service.continuations.pause(claim, code="cancelled")


def test_migration_eleven_is_additive(tmp_path):
    assert [migration.version for migration in CONTINUATION_MIGRATIONS] == [10, 11]
    database = SQLiteDatabase(tmp_path / "old.sqlite3")
    database.initialize(CONTINUATION_MIGRATIONS[:1])
    with database.connect() as connection:
        old_checksum = connection.execute("SELECT checksum FROM schema_migrations WHERE version=10").fetchone()[0]
    repository = ContinuationRepository(database)
    repository.initialize()
    repository.initialize()
    assert database.schema_version == 11
    with database.connect() as connection:
        assert connection.execute("SELECT checksum FROM schema_migrations WHERE version=10").fetchone()[0] == old_checksum


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "succeeded"])
async def test_unclaimed_or_successful_request_cannot_be_quarantined(paused, state):
    service, view, _ = paused
    kernel, request, _, intent = await prepared_intent(paused)
    record = service.continuations.register(intent)
    if state == "succeeded":
        await kernel.run_single(view.task_id, request)
        record = service.continuations.get(intent.request_id)
    before = runtime_tests.state(service, view)
    client = TestClient(create_app(task_service=service))
    assert client.post(url(view, record) + "/quarantine", json=body(paused, record)).status_code == 409
    assert runtime_tests.state(service, view) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["missing_task", "other_task", "missing_request"])
async def test_missing_or_cross_task_request_does_not_leak(paused, scope):
    service, view, _ = paused
    _, _, _, claim = await claim_fixture(paused)
    task_id = view.task_id
    request_id = claim.receipt.request.request_id
    if scope == "missing_task":
        task_id = uuid4()
    elif scope == "other_task":
        task = Task(issue="different", repository_path=view.repository_path)
        service.tasks.create(task)
        task_id = task.id
    else:
        request_id = uuid4()
    client = TestClient(create_app(task_service=service))
    endpoint = f"/api/v1/tasks/{task_id}/continuations/{request_id}"
    assert client.get(endpoint).status_code == 404
    assert client.post(endpoint + "/quarantine", json=body(paused, claim)).status_code == 404
    assert service.continuations.status(task_id=view.task_id, request_id=claim.receipt.request.request_id).quarantine is None


@pytest.mark.asyncio
async def test_quarantine_between_agent_output_and_final_commit_fences_actual_late_turn(paused, monkeypatch):
    service, view, agents = paused
    kernel, request = await runtime_tests.intent(paused)
    before = runtime_tests.state(service, view)
    before_usage = usage(paused)
    finish = service.continuations.finish
    external = ContinuationRepository(SQLiteDatabase(service.tasks.database.path))

    def interleaved(claim, **kwargs):
        human = next(m for m in service.rooms.get_room(claim.receipt.request.room_id).members if m.role is MemberRole.HUMAN)
        external.quarantine(task_id=view.task_id, request_id=claim.receipt.request.request_id,
                            human_member_id=human.member_id,
                            command=QuarantineContinuationRequest(**body(paused, claim)))
        return finish(claim, **kwargs)

    monkeypatch.setattr(service.continuations, "finish", interleaved)
    with pytest.raises(ContinuationConflictError, match="quarantined"):
        await kernel.run_single(view.task_id, request)
    record = service.continuations.active_for_task(view.task_id)
    assert record.receipt.state is ContinuationState.CLAIMED
    assert service.contexts.get(view.task_id) == before[1]
    assert service.tasks.get(view.task_id) == before[0]
    assert service.rooms.get_message(request.message_id).deliveries[0].status.value == "pending"
    assert usage(paused).agent_turns == before_usage.agent_turns + 1
    assert (await kernel.run_single(view.task_id, request)).replayed
    assert [len(a.requests) for a in agents] == [2, 1, 1]
    assert external.status(task_id=view.task_id, request_id=record.receipt.request.request_id).quarantine is not None


@pytest.mark.asyncio
async def test_replay_returns_original_decision_even_after_current_revisions_advance(paused):
    service, view, _ = paused
    _, _, _, claim = await claim_fixture(paused)
    client = TestClient(create_app(task_service=service))
    endpoint = url(view, claim)
    command = body(paused, claim)
    first = client.post(endpoint + "/quarantine", json=command).json()
    snapshot = service.tasks.get(view.task_id)
    service.tasks.save(snapshot.task, expected_revision=snapshot.revision)
    context = service.contexts.get(view.task_id)
    service.contexts.save(context.context, expected_revision=context.revision)
    before_usage = usage(paused)
    assert client.post(endpoint + "/quarantine", json=command).json() == first
    status = client.get(endpoint).json()
    assert status["task_revision"] == command["expected_revision"] + 1
    assert status["runtime_revision"] == command["expected_runtime_revision"] + 1
    assert status["quarantine"] == first and usage(paused) == before_usage


@pytest.mark.asyncio
async def test_success_commit_winning_race_cannot_be_relabelled_as_quarantined(paused, monkeypatch):
    service, view, _ = paused
    kernel, request = await runtime_tests.intent(paused)
    finish = service.continuations.finish
    external = ContinuationRepository(SQLiteDatabase(service.tasks.database.path))

    def interleaved(claim, **kwargs):
        command = QuarantineContinuationRequest(**body(paused, claim))
        human = next(m for m in service.rooms.get_room(claim.receipt.request.room_id).members if m.role is MemberRole.HUMAN)
        receipt = finish(claim, **kwargs)
        with pytest.raises(ContinuationConflictError, match="state or timestamp"):
            external.quarantine(task_id=view.task_id, request_id=claim.receipt.request.request_id,
                                human_member_id=human.member_id, command=command)
        return receipt

    monkeypatch.setattr(service.continuations, "finish", interleaved)
    turn = await kernel.run_single(view.task_id, request)
    assert turn.receipt.state is ContinuationState.SUCCEEDED
    assert external.status(task_id=view.task_id, request_id=turn.receipt.request.request_id).quarantine is None
    assert not service.router.trace_store.list(trace_id=view.trace_id, type=TraceEventType.CONTINUATION_QUARANTINED)


@pytest.mark.asyncio
async def test_nonhuman_cannot_authorize_repository_quarantine(paused):
    service, view, _ = paused
    _, _, _, claim = await claim_fixture(paused)
    with pytest.raises(ContinuationConflictError, match="Human identity"):
        service.continuations.quarantine(
            task_id=view.task_id, request_id=claim.receipt.request.request_id,
            human_member_id=claim.receipt.request.target_member_id,
            command=QuarantineContinuationRequest(**body(paused, claim)),
        )
    assert service.continuations.status(task_id=view.task_id, request_id=claim.receipt.request.request_id).quarantine is None


@pytest.mark.asyncio
async def test_multiple_humans_make_authorization_ambiguous(paused):
    service, view, _ = paused
    _, _, _, claim = await claim_fixture(paused)
    with service.tasks.database.transaction() as connection:
        connection.execute("INSERT INTO room_members VALUES (?, ?, ?, ?, ?, ?)", (
            str(uuid4()), str(claim.receipt.request.room_id), "other-human", "human", "human", utc_now().isoformat(),
        ))
    client = TestClient(create_app(task_service=service))
    assert client.post(url(view, claim) + "/quarantine", json=body(paused, claim)).status_code == 409
    assert service.continuations.status(task_id=view.task_id, request_id=claim.receipt.request.request_id).quarantine is None


@pytest.mark.asyncio
async def test_different_decisions_racing_cannot_both_be_accepted(paused):
    service, view, _ = paused
    _, _, _, claim = await claim_fixture(paused)
    human = next(m for m in service.rooms.get_room(claim.receipt.request.room_id).members if m.role is MemberRole.HUMAN)
    repositories = [ContinuationRepository(SQLiteDatabase(service.tasks.database.path)) for _ in range(3)]
    outcomes = await asyncio.gather(*(asyncio.to_thread(
        repo.quarantine, task_id=view.task_id, request_id=claim.receipt.request.request_id,
        human_member_id=human.member_id,
        command=QuarantineContinuationRequest(**body(paused, claim, reason=f"Containment decision {i}")),
    ) for i, repo in enumerate(repositories)), return_exceptions=True)
    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, ContinuationConflictError) for outcome in outcomes) == 2


def test_openapi_exposes_only_containment_not_execution_authority():
    schema = create_app().openapi()
    schemas = schema["components"]["schemas"]
    properties = schemas["QuarantineContinuationRequest"]["properties"]
    assert schemas["QuarantineContinuationRequest"]["additionalProperties"] is False
    assert "human_member_id" not in properties and "claim_token" not in properties
    assert properties["disposition"]["const"] == "quarantine"
    path = "/api/v1/tasks/{task_id}/continuations/{request_id}"
    assert "get" in schema["paths"][path]
    assert schema["paths"][path + "/quarantine"]["post"]["responses"]["200"]
    assert "/api/v1/tasks/{task_id}/continue" not in schema["paths"]


def test_unconfigured_quarantine_is_unavailable():
    client = TestClient(create_app())
    endpoint = f"/api/v1/tasks/{uuid4()}/continuations/{uuid4()}"
    assert client.get(endpoint).status_code == 503
    response = client.post(endpoint + "/quarantine", json={
        "idempotency_key": str(uuid4()), "expected_revision": 1,
        "expected_runtime_revision": 1, "expected_claim_updated_at": utc_now().isoformat(), "reason": "uncertain",
    })
    assert response.status_code == 503
