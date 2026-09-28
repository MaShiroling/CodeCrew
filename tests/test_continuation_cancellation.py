"""Offline service-owned cancellation, with explicit unknown stop outcomes."""

import asyncio
import sqlite3
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from app.agents import TokenUsage
from app.main import create_app
from app.storage import SQLiteDatabase
from app.storage.continuation_cancellations import ContinuationCancellationRepository
from app.storage.continuations import ContinuationState
from app.trace import TraceEventType
from tests import test_continuation_runtime as runtime_tests
from tests.test_continuation_claims import prepared_intent

paused = runtime_tests.paused


async def active(paused, monkeypatch, *, during_start=False):
    service, view, agents = paused
    kernel, request = await runtime_tests.intent(paused)
    agents[0]._scenario = replace(agents[0]._scenario, block_until_cancel=True)
    signal = asyncio.Event()
    start = agents[0].start

    async def signalled(request):
        if during_start:
            signal.set()
            await asyncio.Event().wait()
        session = await start(request)
        signal.set()
        return session

    monkeypatch.setattr(agents[0], "start", signalled)
    operation = asyncio.create_task(kernel.run_single(view.task_id, request))
    await asyncio.wait_for(signal.wait(), timeout=2)
    claim = service.continuations.active_for_task(view.task_id)
    assert service._lock.locked() and claim.receipt.state is ContinuationState.CLAIMED
    return kernel, request, operation, claim


def body(service, view, claim, **updates):
    return {"idempotency_key": str(uuid4()), "expected_revision": view.revision,
            "expected_runtime_revision": service.contexts.get(view.task_id).revision,
            "expected_claim_updated_at": claim.updated_at.isoformat(), "reason": "Stop this local continuation", **updates}


def client(service):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(task_service=service)), base_url="http://codecrew.test")


def url(view, claim):
    return f"/api/v1/tasks/{view.task_id}/continuations/{claim.receipt.request.request_id}"


@pytest.mark.asyncio
@pytest.mark.parametrize("during_start", [False, True])
async def test_service_cancel_bypasses_turn_lock_and_preserves_scope_budget_and_claim(paused, monkeypatch, during_start):
    service, view, agents = paused
    before = runtime_tests.state(service, view)
    kernel, request, operation, claim = await active(paused, monkeypatch, during_start=during_start)
    command = body(service, view, claim)
    endpoint = url(view, claim)
    async with client(service) as api:
        accepted = await asyncio.wait_for(api.post(endpoint + "/cancel", json=command), timeout=1)
        assert accepted.status_code == 202 and accepted.json()["state"] == "requested"
        assert accepted.json()["external_process_stopped_confirmed"] is False
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, timeout=2)
        observed = (await api.get(endpoint + "/cancellation")).json()
        assert observed["state"] == "observed"
        control = (await api.get(f"/api/v1/tasks/{view.task_id}/control")).json()
        assert control["latest_cancellation"] == observed
        assert control["latest_continuation"]["receipt"]["state"] == "needs_human"
        evidence = observed["observation"]
        assert evidence["outcome"] == ("no_session" if during_start else "adapter_terminal_result")
        if not during_start:
            assert evidence["exit_reason"] == "cancelled"
            artifact = service.router.artifacts.read_json(evidence["result_artifact"]["artifact_id"])
            assert artifact["trace_id"] == str(view.trace_id)
            assert artifact["session_id"] == evidence["session_id"]
        assert "owner_token" not in str(observed) and "claim_token" not in str(observed)
        assert (await api.post(endpoint + "/cancel", json=command)).json() == observed
        changed = {**command, "reason": "different"}
        assert (await api.post(endpoint + "/cancel", json=changed)).status_code == 409
    assert service.tasks.get(view.task_id) == before[0]
    assert service.contexts.get(view.task_id) == before[1]
    assert service.rooms.get_message(request.message_id).deliveries[0].status.value == "pending"
    assert service.continuations.active_for_task(view.task_id).receipt.failure_code == "cancelled"
    assert not service._continuation_runs and not service._lock.locked()
    assert (await kernel.run_single(view.task_id, request)).replayed
    assert [len(a.requests) for a in agents] == ([1, 1, 1] if during_start else [2, 1, 1])
    reopened = ContinuationCancellationRepository(service.continuations)
    assert reopened.get(task_id=view.task_id, request_id=claim.receipt.request.request_id).model_dump(mode="json") == observed
    for type in (TraceEventType.CONTINUATION_CANCEL_REQUESTED, TraceEventType.CONTINUATION_CANCEL_OBSERVED):
        assert len(service.router.trace_store.list(trace_id=view.trace_id, type=type)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,expected", [
    ("cancel_error", "cleanup_failed"), ("hang", "cleanup_timed_out"),
    ("wrong_result", "invalid_result"),
])
async def test_failed_cleanup_remains_unknown_without_false_stop_or_retry(paused, monkeypatch, fault, expected):
    service, view, agents = paused
    service.continuation_cancellation_timeout_seconds = 0.05
    _, _, operation, claim = await active(paused, monkeypatch)
    original_cancel = agents[0].cancel
    original_wait = agents[0].wait

    async def cancel(session_id):
        if fault == "cancel_error":
            raise RuntimeError("cannot cancel")
        if fault == "hang":
            await asyncio.Event().wait()
        await original_cancel(session_id)

    async def wrong_wait(session_id):
        return (await original_wait(session_id)).model_copy(update={"trace_id": uuid4()})

    monkeypatch.setattr(agents[0], "cancel", cancel)
    if fault == "wrong_result":
        monkeypatch.setattr(agents[0], "wait", wrong_wait)
    try:
        async with client(service) as api:
            assert (await api.post(url(view, claim) + "/cancel", json=body(service, view, claim))).status_code == 202
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(operation, timeout=1)
            receipt = (await api.get(url(view, claim) + "/cancellation")).json()
            assert receipt["observation"]["outcome"] == expected
            assert receipt["observation"]["result_artifact"] is None
            assert receipt["external_process_stopped_confirmed"] is False
            assert receipt["claim_released"] is False
    finally:
        # Explicit test cleanup of the still-running Fake, not production proof.
        for session in agents[0]._sessions:
            await original_cancel(session)


@pytest.mark.asyncio
async def test_restarted_unknown_claim_cannot_cancel_arbitrary_external_process(paused):
    service, view, _ = paused
    _, _, _, intent = await prepared_intent(paused)
    service.continuations.register(intent)
    claim = service.continuations.claim(intent.request_id)
    assert not service._continuation_runs
    async with client(service) as api:
        response = await api.post(url(view, claim) + "/cancel", json=body(service, view, claim))
        assert response.status_code == 409
        assert (await api.get(url(view, claim) + "/cancellation")).status_code == 404
    assert service.continuations.get(intent.request_id) == claim


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("expected_revision", True), ("expected_runtime_revision", "1"),
    ("reason", " "), ("claim_released", True), ("pid", 1), ("human_member_id", str(uuid4())),
])
async def test_invalid_or_expanded_cancel_authority_is_rejected(paused, monkeypatch, field, value):
    service, view, _ = paused
    _, _, operation, claim = await active(paused, monkeypatch)
    try:
        async with client(service) as api:
            response = await api.post(url(view, claim) + "/cancel", json=body(service, view, claim, **{field: value}))
            assert response.status_code == 422
        assert not operation.done()
    finally:
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation


@pytest.mark.asyncio
async def test_service_shutdown_can_stop_continuation_without_waiting_for_turn_lock(paused, monkeypatch):
    service, _, _ = paused
    _, _, operation, _ = await active(paused, monkeypatch)
    await asyncio.wait_for(service.shutdown(), timeout=2)
    assert operation.cancelled() and not service._continuation_runs


@pytest.mark.asyncio
async def test_cancel_before_runner_entry_records_unknown_observation_without_launch(paused, monkeypatch):
    service, view, agents = paused
    kernel, request = await runtime_tests.intent(paused)
    started = asyncio.Event()

    async def blocked(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service.event_loop.executor.turns, "run", blocked)
    operation = asyncio.create_task(kernel.run_single(view.task_id, request))
    await asyncio.wait_for(started.wait(), timeout=2)
    claim = service.continuations.active_for_task(view.task_id)
    async with client(service) as api:
        assert (await api.post(url(view, claim) + "/cancel", json=body(service, view, claim))).status_code == 202
        with pytest.raises(asyncio.CancelledError):
            await operation
        observed = (await api.get(url(view, claim) + "/cancellation")).json()
        assert observed["observation"]["outcome"] == "no_observation"
        assert observed["external_process_stopped_confirmed"] is False
    assert [len(a.requests) for a in agents] == [1, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["request", "observation"])
async def test_trace_failure_cannot_create_false_cancel_or_stop_receipt(paused, monkeypatch, phase):
    service, view, _ = paused
    _, _, operation, claim = await active(paused, monkeypatch)
    append = service.continuations.traces.append_in_transaction
    target = TraceEventType.CONTINUATION_CANCEL_REQUESTED if phase == "request" else TraceEventType.CONTINUATION_CANCEL_OBSERVED

    def fail(connection, event):
        if event.type is target:
            raise sqlite3.OperationalError("trace unavailable")
        return append(connection, event)

    monkeypatch.setattr(service.continuations.traces, "append_in_transaction", fail)
    try:
        async with client(service) as api:
            accepted = await api.post(url(view, claim) + "/cancel", json=body(service, view, claim))
            if phase == "request":
                assert accepted.status_code == 503 and not operation.done()
                assert (await api.get(url(view, claim) + "/cancellation")).status_code == 404
            else:
                assert accepted.status_code == 202
                with pytest.raises(asyncio.CancelledError):
                    await operation
                pending = (await api.get(url(view, claim) + "/cancellation")).json()
                assert pending["state"] == "requested" and pending["observation"] is None
                assert pending["external_process_stopped_confirmed"] is False
    finally:
        if not operation.done():
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation


@pytest.mark.asyncio
async def test_persisted_cancel_fences_adapter_that_swallows_coroutine_cancellation(paused, monkeypatch):
    service, view, agents = paused
    kernel, request = await runtime_tests.intent(paused)
    ready = asyncio.Event()
    start = agents[0].start

    async def swallowing(request):
        ready.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return await start(request)  # Intentional broken adapter fixture.

    monkeypatch.setattr(agents[0], "start", swallowing)
    before = service.contexts.get(view.task_id)
    operation = asyncio.create_task(kernel.run_single(view.task_id, request))
    await asyncio.wait_for(ready.wait(), timeout=2)
    claim = service.continuations.active_for_task(view.task_id)
    async with client(service) as api:
        assert (await api.post(url(view, claim) + "/cancel", json=body(service, view, claim))).status_code == 202
        with pytest.raises(asyncio.CancelledError):
            await operation
        observation = (await api.get(url(view, claim) + "/cancellation")).json()["observation"]
        assert observation["outcome"] == "no_observation"
    assert service.contexts.get(view.task_id) == before
    assert service.rooms.get_message(request.message_id).deliveries[0].status.value == "pending"
    assert service.continuations.get(claim.receipt.request.request_id).receipt.failure_code == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["task_revision", "runtime_revision", "claim_timestamp", "task_scope"])
async def test_stale_or_cross_task_cancel_does_not_signal_owned_run(paused, monkeypatch, field):
    service, view, _ = paused
    _, _, operation, claim = await active(paused, monkeypatch)
    command = body(service, view, claim)
    endpoint = url(view, claim)
    if field == "task_revision":
        command["expected_revision"] += 1
    elif field == "runtime_revision":
        command["expected_runtime_revision"] += 1
    elif field == "claim_timestamp":
        command["expected_claim_updated_at"] = "2000-01-01T00:00:00Z"
    else:
        endpoint = f"/api/v1/tasks/{uuid4()}/continuations/{claim.receipt.request.request_id}"
    try:
        async with client(service) as api:
            response = await api.post(endpoint + "/cancel", json=command)
            assert response.status_code == (404 if field == "task_scope" else 409)
        assert not operation.done()
    finally:
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["owner", "json"])
async def test_corrupt_cancel_receipt_fails_closed_on_read_and_replay(paused, monkeypatch, fault):
    service, view, _ = paused
    _, _, operation, claim = await active(paused, monkeypatch)
    command = body(service, view, claim)
    async with client(service) as api:
        assert (await api.post(url(view, claim) + "/cancel", json=command)).status_code == 202
        with pytest.raises(asyncio.CancelledError):
            await operation
        with service.tasks.database.transaction() as connection:
            if fault == "owner":
                connection.execute("UPDATE continuation_cancellations SET owner_token=?", (str(uuid4()),))
            else:
                connection.execute("UPDATE continuation_cancellations SET receipt_json='{}'")
        assert (await api.get(url(view, claim) + "/cancellation")).status_code == 503
        assert (await api.post(url(view, claim) + "/cancel", json=command)).status_code == 503


@pytest.mark.asyncio
async def test_replay_during_cleanup_does_not_interrupt_it_with_second_cancel(paused, monkeypatch):
    service, view, agents = paused
    _, _, operation, claim = await active(paused, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original_cancel = agents[0].cancel
    calls = 0

    async def slow_cancel(session_id):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        await original_cancel(session_id)

    monkeypatch.setattr(agents[0], "cancel", slow_cancel)
    command = body(service, view, claim)
    async with client(service) as api:
        first = await api.post(url(view, claim) + "/cancel", json=command)
        await asyncio.wait_for(entered.wait(), timeout=1)
        for _ in range(3):
            assert (await api.post(url(view, claim) + "/cancel", json=command)).json() == first.json()
        assert operation.cancelling() == 1 and calls == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation


@pytest.mark.asyncio
async def test_cancelled_terminal_result_preserves_known_native_token_usage(paused, monkeypatch):
    service, view, agents = paused
    room_id = service.contexts.get(view.task_id).context.room_id
    guard = service.event_loop.executor.budget_guard
    before = guard.usage(view.task_id, room_id=room_id)
    _, _, operation, claim = await active(paused, monkeypatch)
    wait = agents[0].wait

    async def reported(session_id):
        return (await wait(session_id)).model_copy(update={"token_usage": TokenUsage(input_tokens=17, output_tokens=3)})

    monkeypatch.setattr(agents[0], "wait", reported)
    async with client(service) as api:
        assert (await api.post(url(view, claim) + "/cancel", json=body(service, view, claim))).status_code == 202
        with pytest.raises(asyncio.CancelledError):
            await operation
    after = guard.usage(view.task_id, room_id=room_id)
    assert after.agent_turns == before.agent_turns + 1
    assert after.reported_total_tokens == before.reported_total_tokens + 20
    assert after.turns_without_token_usage == before.turns_without_token_usage


def test_cancel_openapi_and_additive_migration(tmp_path):
    schema = create_app().openapi()
    path = "/api/v1/tasks/{task_id}/continuations/{request_id}"
    assert "202" in schema["paths"][path + "/cancel"]["post"]["responses"]
    assert schema["components"]["schemas"]["CancelContinuationRequest"]["additionalProperties"] is False
    from app.storage.continuations import ContinuationRepository
    claims = ContinuationRepository(SQLiteDatabase(tmp_path / "migration.sqlite3"))
    claims.initialize()
    repo = ContinuationCancellationRepository(claims)
    repo.initialize()
    repo.initialize()
    assert repo.database.schema_version == 12
