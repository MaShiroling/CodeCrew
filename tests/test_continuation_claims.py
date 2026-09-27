"""Durable claims/receipts on real SQLite; Fake turns, never paid models."""

import asyncio
import copy
import json
import subprocess
import sys
from uuid import uuid4, uuid5

import pytest
from pydantic import ValidationError

from app.api.continuation_runtime import HumanContinuationKernel
from app.api.service import TaskStateConflict
from app.storage import SQLiteDatabase
from app.storage.continuations import (
    ContinuationConflictError,
    ContinuationIntegrityError,
    ContinuationIntent,
    ContinuationReceipt,
    ContinuationRepository,
    ContinuationState,
    human_message_digest,
)
from app.team import MemberRole
from app.trace import TraceEventType
from tests import test_continuation_runtime as runtime_tests

paused = runtime_tests.paused


async def prepared_intent(paused, *, recipient="planner", target=None):
    _, view, _ = paused
    kernel, request = await runtime_tests.intent(paused, recipient=recipient, target=target)
    prepared = await kernel.prepare(view.task_id, request)
    checkpoint = prepared.checkpoint
    key = uuid5(view.task_id, f"human-continuation:{request.message_id}")
    intent = ContinuationIntent(
        idempotency_key=key, task_id=view.task_id, trace_id=view.trace_id,
        room_id=prepared.runtime.room_id, message_id=request.message_id,
        source_sha256=human_message_digest(prepared.source.message.model_dump(mode="json")),
        correlation_id=checkpoint.correlation_id, target_role=request.target_role.value,
        target_member_id=checkpoint.target_member_id,
        source_recipient_id=prepared.source.deliveries[0].recipient_id,
        agent_name=prepared.runtime.agent_names[request.target_role],
        expected_revision=checkpoint.task_revision, runtime_revision=checkpoint.runtime_revision,
    )
    return kernel, request, prepared, intent


@pytest.mark.asyncio
async def test_migration_registration_trace_and_reopen_are_idempotent(paused):
    service, view, agents = paused
    _, _, _, intent = await prepared_intent(paused)
    repository = service.continuations
    first = repository.register(intent)
    reopened = ContinuationRepository(SQLiteDatabase(repository.database.path))
    reopened.initialize()
    assert reopened.database.schema_version == 10
    assert reopened.register(intent.model_copy(update={"request_id": uuid4()})) == first
    assert reopened.get_by_key(view.task_id, intent.idempotency_key) == first
    events = service.router.trace_store.list(trace_id=view.trace_id, type=TraceEventType.CONTINUATION_REQUESTED)
    assert len(events) == 1 and events[0].event.correlation_id == intent.correlation_id
    assert repository.active_for_task(view.task_id) == first
    assert [len(a.requests) for a in agents] == [1, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["message", "role", "revision"])
async def test_same_key_different_client_command_conflicts(paused, change):
    service, _, _ = paused
    _, _, _, intent = await prepared_intent(paused)
    first = service.continuations.register(intent)
    update = {"message_id": uuid4()} if change == "message" else (
        {"target_role": "reviewer"} if change == "role" else {"expected_revision": intent.expected_revision + 1}
    )
    changed = ContinuationIntent.model_validate({**intent.model_dump(), **update})
    with pytest.raises(ContinuationConflictError, match="conflicts"):
        service.continuations.register(changed)
    assert service.continuations.get(first.receipt.request.request_id) == first


@pytest.mark.asyncio
async def test_new_key_cannot_reuse_human_message_or_overlap_task_reservation(paused):
    service, view, _ = paused
    _, _, _, intent = await prepared_intent(paused, recipient="orchestrator")
    first = service.continuations.register(intent)
    with pytest.raises(ContinuationConflictError, match="reservation"):
        service.continuations.register(intent.model_copy(update={"request_id": uuid4(), "idempotency_key": uuid4()}))
    # A different Human message still cannot run while this task is reserved.
    kernel, different = await runtime_tests.intent(paused, recipient="reviewer")
    with pytest.raises(ContinuationConflictError, match="reservation"):
        await kernel.run_single(view.task_id, different, idempotency_key=uuid4())
    assert service.continuations.active_for_task(view.task_id) == first


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["task_revision", "runtime_revision", "target", "trace", "room", "correlation", "recipient", "agent", "acked"])
async def test_registration_rechecks_scope_in_transaction_without_inserting(paused, fault):
    service, view, _ = paused
    _, _, _, intent = await prepared_intent(paused)
    changes = {
        "task_revision": {"expected_revision": intent.expected_revision + 1},
        "runtime_revision": {"runtime_revision": intent.runtime_revision + 1},
        "target": {"target_member_id": uuid4()}, "trace": {"trace_id": uuid4()},
        "room": {"room_id": uuid4()}, "correlation": {"correlation_id": uuid4()},
        "recipient": {"source_recipient_id": uuid4()}, "agent": {"agent_name": "unbound"},
    }
    if fault == "acked":
        service.rooms.acknowledge(intent.message_id, recipient_id=intent.source_recipient_id)
    else:
        intent = intent.model_copy(update=changes[fault])
    with pytest.raises(ContinuationConflictError):
        service.continuations.register(intent)
    assert service.continuations.get_by_key(view.task_id, intent.idempotency_key) is None
    assert not service.router.trace_store.list(trace_id=view.trace_id, type=TraceEventType.CONTINUATION_REQUESTED)


@pytest.mark.asyncio
async def test_independent_connections_race_to_exactly_one_claim(paused):
    service, view, agents = paused
    _, _, _, intent = await prepared_intent(paused)
    service.continuations.register(intent)
    repositories = [ContinuationRepository(SQLiteDatabase(service.tasks.database.path)) for _ in range(6)]
    results = await asyncio.gather(*(asyncio.to_thread(repo.claim, intent.request_id) for repo in repositories))
    claims = [record for record in results if record is not None]
    assert len(claims) == 1 and claims[0].claim_token is not None
    assert claims[0].receipt.state is ContinuationState.CLAIMED
    assert service.rooms.get_message(intent.message_id).deliveries[0].status.value == "pending"
    assert len(service.router.trace_store.list(trace_id=view.trace_id, type=TraceEventType.CONTINUATION_CLAIMED)) == 1
    assert [len(a.requests) for a in agents] == [1, 1, 1]


@pytest.mark.asyncio
async def test_independent_service_locks_racing_prepare_replay_one_claim(paused, monkeypatch):
    service, view, agents = paused
    kernel, request = await runtime_tests.intent(paused)
    other_service = copy.copy(service)
    other_service._lock = asyncio.Lock()
    other_service.continuations = ContinuationRepository(SQLiteDatabase(service.tasks.database.path))
    second_inspection = asyncio.Event()
    inspect = service.worktrees.inspect
    calls = 0

    async def interleaved(task_id):
        nonlocal calls
        handle = await inspect(task_id)
        calls += 1
        if calls == 1:
            await asyncio.wait_for(second_inspection.wait(), timeout=2)
        else:
            second_inspection.set()
        return handle

    monkeypatch.setattr(service.worktrees, "inspect", interleaved)
    outcomes = await asyncio.wait_for(asyncio.gather(
        kernel.run_single(view.task_id, request),
        HumanContinuationKernel(other_service).run_single(view.task_id, request),
    ), timeout=5)
    assert sum(outcome.replayed for outcome in outcomes) == 1
    assert {outcome.receipt.state for outcome in outcomes} <= {
        ContinuationState.CLAIMED, ContinuationState.SUCCEEDED,
    }
    assert len({outcome.receipt.request.request_id for outcome in outcomes}) == 1
    assert [len(a.requests) for a in agents] == [2, 1, 1]


@pytest.mark.asyncio
async def test_wrong_owner_and_terminal_state_cannot_finish_or_reclaim(paused):
    service, _, _ = paused
    _, _, _, intent = await prepared_intent(paused)
    repository = service.continuations
    repository.register(intent)
    claim = repository.claim(intent.request_id)
    with pytest.raises(ContinuationConflictError, match="owned"):
        repository.pause(claim.model_copy(update={"claim_token": uuid4()}), code="cancelled")
    receipt = repository.pause(claim, code="execution_failed")
    assert receipt.state is ContinuationState.NEEDS_HUMAN
    assert repository.claim(intent.request_id) is None
    with pytest.raises(ContinuationConflictError, match="owned"):
        repository.pause(claim, code="cancelled")


@pytest.mark.asyncio
async def test_unclaimed_pending_request_can_resume_but_claimed_request_never_dispatches_again(paused):
    service, view, agents = paused
    kernel, request, _, intent = await prepared_intent(paused)
    service.continuations.register(intent)
    # Persisted pending was never executed; the same explicit call can claim it.
    result = await kernel.run_single(view.task_id, request)
    assert not result.replayed and result.receipt.state is ContinuationState.SUCCEEDED
    snapshot = runtime_tests.state(service, view)
    usage = service.event_loop.executor.budget_guard.usage(view.task_id, room_id=intent.room_id)
    service.continuations = ContinuationRepository(SQLiteDatabase(service.tasks.database.path))
    replay = await HumanContinuationKernel(service).run_single(view.task_id, request)
    assert replay.replayed and replay.receipt == result.receipt
    assert runtime_tests.state(service, view) == snapshot
    assert service.event_loop.executor.budget_guard.usage(view.task_id, room_id=intent.room_id) == usage
    assert [len(a.requests) for a in agents] == [2, 1, 1]


@pytest.mark.asyncio
async def test_real_process_restart_keeps_claim_consumed(paused):
    service, view, agents = paused
    kernel, request, _, intent = await prepared_intent(paused)
    service.continuations.register(intent)
    script = (
        "import sys; from pathlib import Path; from uuid import UUID; "
        "from app.storage import SQLiteDatabase; "
        "from app.storage.continuations import ContinuationRepository; "
        "r=ContinuationRepository(SQLiteDatabase(Path(sys.argv[1]))); "
        "c=r.claim(UUID(sys.argv[2])); print(c.receipt.state.value)"
    )
    process = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script,
                                     str(service.tasks.database.path), str(intent.request_id)],
                                    capture_output=True, text=True, check=True)
    assert process.stdout.strip() == "claimed"
    receipt = await kernel.run_single(view.task_id, request)
    assert receipt.replayed and receipt.receipt.state is ContinuationState.CLAIMED
    assert receipt.result is None and receipt.receipt.agent_session_id is None
    assert [len(a.requests) for a in agents] == [1, 1, 1]
    with pytest.raises(TaskStateConflict, match="reservation"):
        await kernel.prepare(view.task_id, request)


@pytest.mark.asyncio
@pytest.mark.parametrize("recipient", ["planner", "orchestrator"])
async def test_atomic_finish_trace_failure_rolls_back_runtime_and_all_acks(paused, monkeypatch, recipient):
    service, view, agents = paused
    kernel, request = await runtime_tests.intent(paused, recipient=recipient)
    before_context = service.contexts.get(view.task_id)
    append = service.continuations.traces.append_in_transaction

    def fail_success(connection, event):
        if event.type is TraceEventType.CONTINUATION_SUCCEEDED:
            raise RuntimeError("injected commit failure")
        return append(connection, event)

    monkeypatch.setattr(service.continuations.traces, "append_in_transaction", fail_success)
    with pytest.raises(RuntimeError, match="commit failure"):
        await kernel.run_single(view.task_id, request)
    assert service.contexts.get(view.task_id) == before_context
    source = service.rooms.get_message(request.message_id)
    assert source.deliveries[0].status.value == "pending"
    target = next(m for m in service.rooms.get_room(before_context.context.room_id).members if m.role is MemberRole.PLANNER)
    handoff = next(message for message in service.rooms.pending_for(target.member_id)
                   if message.message.causation_id == request.message_id and message.message.sender_id != source.message.sender_id)
    assert all(d.status.value == "pending" for d in handoff.deliveries)
    replay = await kernel.run_single(view.task_id, request)
    assert replay.replayed and replay.receipt.state is ContinuationState.NEEDS_HUMAN
    assert replay.receipt.failure_code == "execution_failed"
    assert replay.receipt.consumed_message_ids == ()
    assert [len(a.requests) for a in agents] == [2, 1, 1]
    assert not service.router.trace_store.list(trace_id=view.trace_id, type=TraceEventType.CONTINUATION_SUCCEEDED)


@pytest.mark.asyncio
async def test_runtime_cas_failure_after_agent_turn_does_not_ack_or_retry(paused, monkeypatch):
    service, view, agents = paused
    kernel, request = await runtime_tests.intent(paused)
    executor = service.event_loop.executor
    execute = executor.execute_single_turn

    async def concurrent_update(**kwargs):
        result = await execute(**kwargs)
        context = service.contexts.get(view.task_id)
        service.contexts.save(context.context, expected_revision=context.revision)
        return result

    monkeypatch.setattr(executor, "execute_single_turn", concurrent_update)
    with pytest.raises(ContinuationConflictError, match="revision"):
        await kernel.run_single(view.task_id, request)
    replay = await kernel.run_single(view.task_id, request)
    assert replay.replayed and replay.receipt.state is ContinuationState.NEEDS_HUMAN
    assert service.rooms.get_message(request.message_id).deliveries[0].status.value == "pending"
    assert [len(a.requests) for a in agents] == [2, 1, 1]


@pytest.mark.asyncio
async def test_repository_corruption_fails_closed_without_model_dispatch(paused):
    service, view, agents = paused
    kernel, request, _, intent = await prepared_intent(paused)
    record = service.continuations.register(intent)
    with service.tasks.database.transaction() as connection:
        body = json.loads(record.model_dump_json())
        body["receipt"]["request"]["trace_id"] = str(uuid4())
        connection.execute("UPDATE continuation_requests SET record_json=? WHERE request_id=?",
                           (json.dumps(body), str(intent.request_id)))
    with pytest.raises(ContinuationIntegrityError):
        await kernel.run_single(view.task_id, request)
    assert [len(a.requests) for a in agents] == [1, 1, 1]


@pytest.mark.asyncio
async def test_stored_human_body_cannot_change_between_registration_and_claim(paused):
    service, view, agents = paused
    _, request, prepared, intent = await prepared_intent(paused)
    service.continuations.register(intent)
    changed = prepared.source.message.model_copy(update={"content": "Substituted instruction"})
    with service.tasks.database.transaction() as connection:
        connection.execute("UPDATE chat_messages SET message_json=? WHERE message_id=?",
                           (changed.model_dump_json(), str(request.message_id)))
    with pytest.raises(ContinuationConflictError, match="Human intent changed"):
        service.continuations.claim(intent.request_id)
    assert service.continuations.get(intent.request_id).receipt.state is ContinuationState.PENDING
    assert [len(a.requests) for a in agents] == [1, 1, 1]
    assert not service.router.trace_store.list(trace_id=view.trace_id, type=TraceEventType.CONTINUATION_CLAIMED)


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [{"expected_revision": True}, {"runtime_revision": "2"}, {"target_role": "human"}, {"reset_budget": True}])
async def test_intent_is_strict_and_extra_fields_are_forbidden(paused, updates):
    _, _, _, intent = await prepared_intent(paused)
    with pytest.raises(ValidationError):
        ContinuationIntent.model_validate({**intent.model_dump(), **updates})


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["succeeded", "needs_human"])
async def test_terminal_receipt_cannot_be_fabricated_without_required_evidence(paused, state):
    _, _, _, intent = await prepared_intent(paused)
    with pytest.raises(ValidationError):
        ContinuationReceipt(request=intent, state=state)
