"""Offline recovery/consumption on real SQLite/Git; no fresh model dispatch."""

import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api.controlled_resumption import ControlledResumptionKernel
from app.api.models import AuthorizeContinuationRequest
from app.api.service import TaskStateConflict
from app.orchestration.models import InvalidTaskTransition, Task, TaskState
from app.storage import SQLiteDatabase
from app.storage.continuation_authorizations import ContinuationAuthorizationRepository
from app.storage.continuation_resumptions import (
    ContinuationResumptionReceipt,
    ContinuationResumptionRepository,
)
from app.storage.continuations import (
    ContinuationConflictError,
    ContinuationIntegrityError,
    ContinuationNotFoundError,
    ContinuationState,
)
from app.team.budgets import ConversationBudgetPolicy
from app.trace import TraceEventType
from tests import test_continuation_authorization as authorization_tests
from tests import test_continuation_runtime as runtime_tests

paused = runtime_tests.paused


async def authorized(paused, role="planner"):
    service, view, _ = paused
    previous, body, _ = await authorization_tests.setup(paused, role)
    grant = await service.authorize_continuation(view.task_id, previous.receipt.request.request_id,
                                                AuthorizeContinuationRequest(**body))
    return ControlledResumptionKernel(service), grant


@pytest.mark.asyncio
@pytest.mark.parametrize("role,state", [("planner", TaskState.PLANNING), ("implementer", TaskState.IMPLEMENTING), ("reviewer", TaskState.VERIFYING)])
async def test_atomic_role_recovery_invalidates_old_certificates_without_dispatch(paused, role, state):
    service, view, agents = paused
    kernel, grant = await authorized(paused, role)
    before = runtime_tests.state(service, view)
    before_usage = authorization_tests.usage(service, view)
    result = await kernel.resume(view.task_id, grant.authorization_id)
    receipt = result.record.receipt
    assert not result.record.replayed
    assert result.record.claim.claim_token is not None
    assert result.record.claim.receipt.state is ContinuationState.CLAIMED
    assert result.record.claim.receipt.request.request_id == grant.intent.request_id
    assert receipt.resumed_state is state
    assert receipt.task_revision == before[0].revision + 1
    assert receipt.runtime_revision == before[1].revision + 1
    assert result.runtime.task.state is state
    assert result.runtime.native_session_ids == {}
    assert result.runtime.latest_verification is None and result.runtime.latest_completion is None
    assert result.historical_references == grant.artifacts
    task = service.tasks.get(view.task_id)
    context = service.contexts.get(view.task_id)
    assert task == result.record.task
    assert context.revision == receipt.runtime_revision and context.context == result.record.context
    assert all(binding.native_session_id is None for binding in context.context.agent_bindings)
    for field in ("id", "trace_id", "issue", "repository_path", "rework_rounds", "metadata", "created_at"):
        assert getattr(task.task, field) == getattr(before[0].task, field)
    assert context.context.worktree == before[1].context.worktree
    assert context.context.verification_plan == before[1].context.verification_plan
    assert service.rooms.list_messages(context.context.room_id) == before[2]
    assert service.rooms.get_message(grant.intent.message_id).deliveries[0].status.value == "pending"
    assert authorization_tests.usage(service, view) == before_usage
    assert [len(agent.requests) for agent in agents] == [2, 1, 1]
    assert receipt.agent_dispatched is False and receipt.agent_dispatch_ready is False
    assert receipt.authorization_consumed is True and receipt.historical_evidence_invalidated is True
    assert "claim_token" not in receipt.model_dump_json()
    events = service.router.trace_store.list(trace_id=view.trace_id, type=TraceEventType.CONTINUATION_RESUMED)
    assert len(events) == 1 and events[0].event.causation_id == grant.intent.message_id
    assert not service.router.trace_store.list(trace_id=view.trace_id, type=TraceEventType.COMPLETION_DECIDED)
    after = runtime_tests.state(service, view)
    replay = await kernel.resume(view.task_id, grant.authorization_id)
    assert replay.record.replayed and replay.record.receipt == receipt
    assert replay.record.claim is None and replay.runtime is None and replay.source is None
    assert replay.historical_references == ()
    assert runtime_tests.state(service, view) == after


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["task_revision", "runtime_revision", "runtime_content", "old_timestamp", "grant_corrupt", "message_corrupt", "wrong_task", "missing_grant"])
async def test_stale_and_corrupt_authority_never_changes_task_or_claims(paused, fault):
    service, view, agents = paused
    kernel, grant = await authorized(paused)
    task_id, authorization_id = view.task_id, grant.authorization_id
    if fault == "task_revision":
        snapshot = service.tasks.get(task_id)
        service.tasks.save(snapshot.task, expected_revision=snapshot.revision)
    elif fault == "runtime_revision":
        context = service.contexts.get(task_id)
        service.contexts.save(context.context, expected_revision=context.revision)
    elif fault == "runtime_content":
        context = service.contexts.get(task_id).context
        changed = context.model_copy(update={"agent_bindings": tuple(b.model_copy(update={"native_session_id": "changed"}) for b in context.agent_bindings)})
        with service.tasks.database.transaction() as connection:
            connection.execute("UPDATE workflow_runtime_contexts SET context_json=? WHERE task_id=?", (changed.model_dump_json(), str(task_id)))
    elif fault == "old_timestamp":
        with service.tasks.database.transaction() as connection:
            connection.execute("UPDATE continuation_requests SET updated_at='corrupt' WHERE request_id=?", (str(grant.previous_request_id),))
    elif fault == "grant_corrupt":
        with service.tasks.database.transaction() as connection:
            connection.execute("UPDATE continuation_authorizations SET receipt_json='{}'")
    elif fault == "message_corrupt":
        with service.tasks.database.transaction() as connection:
            connection.execute("UPDATE chat_messages SET message_json='{}' WHERE message_id=?", (str(grant.intent.message_id),))
    elif fault == "wrong_task":
        task_id = uuid4()
    else:
        authorization_id = uuid4()
    task_before = service.tasks.get(view.task_id)
    context_before = service.contexts.get(view.task_id)
    with pytest.raises((ContinuationConflictError, ContinuationIntegrityError, ContinuationNotFoundError, TaskStateConflict)):
        await kernel.resume(task_id, authorization_id)
    assert service.tasks.get(view.task_id) == task_before
    assert service.contexts.get(view.task_id) == context_before
    assert service.continuations.active_for_task(view.task_id) is None
    assert [len(agent.requests) for agent in agents] == [2, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["budget_turns", "budget_messages", "rework", "policy", "registry", "worktree", "plan_blob", "closed_room"])
async def test_budget_permissions_and_evidence_revalidated_at_consume(paused, fault):
    service, view, agents = paused
    kernel, grant = await authorized(paused)
    if fault == "budget_turns":
        service.event_loop.executor.budget_guard.policy = ConversationBudgetPolicy(max_agent_turns=1)
    elif fault == "budget_messages":
        guard = service.event_loop.executor.budget_guard
        guard.policy = ConversationBudgetPolicy(max_room_messages=authorization_tests.usage(service, view).room_messages + 1)
    elif fault == "rework":
        service.event_loop.controller.max_rework_rounds = 0
    elif fault == "policy":
        service.verification_plan = service.verification_plan.model_copy(update={"commands": ()})
    elif fault == "registry":
        service.event_loop.executor.turns.registry.unregister(agents[0].name)
    elif fault == "worktree":
        service.worktrees._manifest_path(view.task_id).unlink()
    elif fault == "plan_blob":
        reference = next(ref for ref in grant.artifacts if ref.type.value == "plan")
        service.router.artifacts.blob_path_for(reference.artifact_id).write_text("changed")
    else:
        with service.tasks.database.transaction() as connection:
            connection.execute("UPDATE team_rooms SET status='closed',closed_at=created_at WHERE room_id=?", (str(grant.intent.room_id),))
    before = runtime_tests.state(service, view)
    from app.agents.registry import AgentRegistryError
    from app.api.service import TaskDetailUnavailable
    from app.storage import ArtifactIntegrityError
    from app.workspace import WorktreeError
    with pytest.raises((TaskStateConflict, TaskDetailUnavailable, AgentRegistryError, ArtifactIntegrityError, WorktreeError)):
        await kernel.resume(view.task_id, grant.authorization_id)
    assert runtime_tests.state(service, view) == before
    assert service.continuations.active_for_task(view.task_id) is None
    assert [len(agent.requests) for agent in agents] == [2, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("type", [TraceEventType.CONTINUATION_REQUESTED, TraceEventType.CONTINUATION_CLAIMED, TraceEventType.CONTINUATION_RESUMED])
async def test_trace_failure_rolls_back_all_state_and_consumption(paused, monkeypatch, type):
    service, view, _ = paused
    kernel, grant = await authorized(paused)
    before = runtime_tests.state(service, view)
    append = service.continuations.traces.append_in_transaction
    def injected(connection, event):
        if event.type is type:
            import sqlite3
            raise sqlite3.OperationalError("injected trace failure")
        return append(connection, event)
    monkeypatch.setattr(service.continuations.traces, "append_in_transaction", injected)
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        await kernel.resume(view.task_id, grant.authorization_id)
    assert runtime_tests.state(service, view) == before
    assert service.continuation_resumptions.get(task_id=view.task_id, authorization_id=grant.authorization_id) is None
    assert service.continuations.active_for_task(view.task_id) is None


@pytest.mark.asyncio
async def test_concurrent_independent_connections_consume_once_without_replay_owner(paused):
    service, view, _ = paused
    _, grant = await authorized(paused)
    repositories = []
    for _ in range(2):
        authorizations = ContinuationAuthorizationRepository(service.continuations)
        authorizations.database = SQLiteDatabase(service.tasks.database.path)
        repositories.append(ContinuationResumptionRepository(authorizations))
    results = await asyncio.gather(*(asyncio.to_thread(repository.consume,
        task_id=view.task_id, authorization_id=grant.authorization_id,
        references=grant.artifacts, validate_budget=lambda: None) for repository in repositories))
    assert sum(not result.replayed for result in results) == 1
    replay = next(result for result in results if result.replayed)
    assert replay.claim is None and replay.task is None and replay.context is None
    assert len(service.router.trace_store.list(trace_id=view.trace_id, type=TraceEventType.CONTINUATION_RESUMED)) == 1


@pytest.mark.asyncio
async def test_unresolved_reservation_prevents_recovery_even_with_a_prior_grant(paused):
    service, view, _ = paused
    kernel, grant = await authorized(paused)
    service.continuations.register(grant.intent)
    before = runtime_tests.state(service, view)
    with pytest.raises(ContinuationConflictError):
        await kernel.resume(view.task_id, grant.authorization_id)
    assert runtime_tests.state(service, view) == before


@pytest.mark.asyncio
async def test_budget_change_after_git_preparation_is_checked_before_commit(paused, monkeypatch):
    service, view, _ = paused
    kernel, grant = await authorized(paused)
    consume = service.continuation_resumptions.consume
    def changed(**kwargs):
        service.event_loop.executor.budget_guard.policy = ConversationBudgetPolicy(max_agent_turns=1)
        return consume(**kwargs)
    monkeypatch.setattr(service.continuation_resumptions, "consume", changed)
    before = runtime_tests.state(service, view)
    with pytest.raises(TaskStateConflict, match="budget blocked"):
        await kernel.resume(view.task_id, grant.authorization_id)
    assert runtime_tests.state(service, view) == before


@pytest.mark.asyncio
async def test_grant_snapshot_rejects_runtime_change_during_git_inspection(paused, monkeypatch):
    service, view, _ = paused
    kernel, grant = await authorized(paused)
    inspect = service.worktrees.inspect
    async def changed(task_id):
        result = await inspect(task_id)
        context = service.contexts.get(task_id)
        service.contexts.save(context.context, expected_revision=context.revision)
        return result
    monkeypatch.setattr(service.worktrees, "inspect", changed)
    before = service.tasks.get(view.task_id)
    with pytest.raises(TaskStateConflict, match="changed during"):
        await kernel.resume(view.task_id, grant.authorization_id)
    assert service.tasks.get(view.task_id) == before
    assert service.continuations.active_for_task(view.task_id) is None


@pytest.mark.asyncio
async def test_repository_rejects_changed_plan_after_preparation(paused, monkeypatch):
    service, view, _ = paused
    kernel, grant = await authorized(paused)
    consume = service.continuation_resumptions.consume
    def changed(**kwargs):
        # Controlled fixture mutates the latest Plan index between preparation
        # and the transaction; evidence previously read cannot authorize it.
        with service.tasks.database.transaction() as connection:
            connection.execute("UPDATE plan_revisions SET artifact_id=? WHERE room_id=?", (str(uuid4()), str(grant.intent.room_id)))
        return consume(**kwargs)
    monkeypatch.setattr(service.continuation_resumptions, "consume", changed)
    before_task = service.tasks.get(view.task_id)
    before_runtime = service.contexts.get(view.task_id)
    with pytest.raises(ContinuationConflictError, match="Plan version"):
        await kernel.resume(view.task_id, grant.authorization_id)
    assert service.tasks.get(view.task_id) == before_task
    assert service.contexts.get(view.task_id) == before_runtime
    assert service.continuations.active_for_task(view.task_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["json", "index", "state", "grant_hash", "claim"])
async def test_corrupt_consumption_never_yields_another_owner(paused, fault):
    service, view, _ = paused
    kernel, grant = await authorized(paused)
    result = await kernel.resume(view.task_id, grant.authorization_id)
    other = service.tasks.create(Task(issue="other", repository_path="/fixture")) if fault == "index" else None
    with service.tasks.database.transaction() as connection:
        if fault == "json":
            connection.execute("UPDATE continuation_resumptions SET receipt_json='{}'")
        elif fault == "index":
            connection.execute("UPDATE continuation_resumptions SET task_id=?", (str(other.task.id),))
        elif fault in {"state", "grant_hash"}:
            fields = {"resumed_state": TaskState.REVIEWING} if fault == "state" else {"authorization_sha256": "0" * 64}
            changed = result.record.receipt.model_copy(update=fields)
            connection.execute("UPDATE continuation_resumptions SET receipt_json=?", (changed.model_dump_json(),))
        else:
            connection.execute("UPDATE continuation_requests SET record_json='{}' WHERE request_id=?", (str(grant.intent.request_id),))
    with pytest.raises(ContinuationIntegrityError):
        await kernel.resume(view.task_id, grant.authorization_id)


@pytest.mark.asyncio
async def test_reopen_replays_only_receipt_and_does_not_reactivate_or_dispatch(paused):
    service, view, agents = paused
    kernel, grant = await authorized(paused)
    result = await kernel.resume(view.task_id, grant.authorization_id)
    repository = ContinuationResumptionRepository(service.continuation_authorizations)
    repository.database = SQLiteDatabase(service.tasks.database.path)
    repository.initialize()
    service.continuation_resumptions = repository
    before = runtime_tests.state(service, view)
    assert (await kernel.resume(view.task_id, grant.authorization_id)).record.receipt == result.record.receipt
    assert runtime_tests.state(service, view) == before
    assert [len(agent.requests) for agent in agents] == [2, 1, 1]
    assert service.continuations.claim(grant.intent.request_id) is None


def coordinator(service):
    from app.recovery import EvidenceRecoveryService, WorkflowRecoveryCoordinator
    return WorkflowRecoveryCoordinator(tasks=service.tasks, contexts=service.contexts,
        rooms=service.rooms, traces=service.router.trace_store, worktrees=service.worktrees,
        evidence=EvidenceRecoveryService(service.router.artifacts, service.router.trace_store),
        event_loop=service.event_loop)


@pytest.mark.asyncio
async def test_startup_parks_a_staged_claim_instead_of_dispatching_room_history(paused):
    from app.recovery import RecoveryDisposition
    service, view, agents = paused
    kernel, grant = await authorized(paused)
    result = await kernel.resume(view.task_id, grant.authorization_id)
    before_usage = authorization_tests.usage(service, view)
    entries = await coordinator(service).scan()
    entry = next(entry for entry in entries if entry.task_id == view.task_id)
    assert entry.disposition is RecoveryDisposition.NEEDS_HUMAN
    assert entry.runtime is None and entry.pending_events == ()
    assert "unresolved continuation" in entry.reason
    assert service.tasks.get(view.task_id).task.state is TaskState.NEEDS_HUMAN
    assert service.continuations.get(grant.intent.request_id) == result.record.claim
    before = runtime_tests.state(service, view)
    replay = await kernel.resume(view.task_id, grant.authorization_id)
    assert replay.record.replayed and replay.runtime is None
    assert runtime_tests.state(service, view) == before
    assert authorization_tests.usage(service, view) == before_usage
    assert [len(agent.requests) for agent in agents] == [2, 1, 1]


@pytest.mark.asyncio
async def test_recovery_resume_rechecks_claim_fence_after_scan(paused):
    from app.recovery import RecoveryDisposition, RecoveryEntry
    from app.recovery.coordinator import RecoveryCoordinatorError
    service, view, agents = paused
    kernel, grant = await authorized(paused)
    result = await kernel.resume(view.task_id, grant.authorization_id)
    # A stale/forged resumable classification cannot bypass the ledger check.
    entry = RecoveryEntry(task_id=view.task_id, disposition=RecoveryDisposition.RESUMABLE,
        reason="test stale classifier", task_revision=result.record.receipt.task_revision,
        runtime_revision=result.record.receipt.runtime_revision, runtime=result.runtime,
        pending_events=(result.source,))
    before = runtime_tests.state(service, view)
    with pytest.raises(RecoveryCoordinatorError, match="unresolved continuation"):
        await coordinator(service).resume(entry)
    assert runtime_tests.state(service, view) == before
    assert [len(agent.requests) for agent in agents] == [2, 1, 1]


@pytest.mark.asyncio
async def test_corrupt_claim_index_cannot_hide_from_startup_fence(paused):
    from app.recovery import RecoveryDisposition
    service, view, agents = paused
    kernel, grant = await authorized(paused)
    await kernel.resume(view.task_id, grant.authorization_id)
    with service.tasks.database.transaction() as connection:
        connection.execute("UPDATE continuation_requests SET state='succeeded' WHERE request_id=?", (str(grant.intent.request_id),))
    entries = await coordinator(service).scan()
    entry = next(entry for entry in entries if entry.task_id == view.task_id)
    assert entry.disposition is RecoveryDisposition.NEEDS_HUMAN
    assert "corrupt" in entry.reason
    assert [len(agent.requests) for agent in agents] == [2, 1, 1]


@pytest.mark.parametrize("state", [TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED, TaskState.CREATED])
def test_other_terminal_or_unpaused_tasks_cannot_use_recovery_primitive(state):
    task = Task(issue="x", repository_path="/fixture", state=state)
    with pytest.raises(InvalidTaskTransition):
        task.resume_for_continuation(TaskState.PLANNING)


@pytest.mark.parametrize("target", [TaskState.REVIEWING, TaskState.COMPLETED, TaskState.REWORK])
def test_recovery_cannot_jump_to_review_completion_or_reset_rework(target):
    task = Task(issue="x", repository_path="/fixture", state=TaskState.NEEDS_HUMAN, rework_rounds=2)
    with pytest.raises(InvalidTaskTransition):
        task.resume_for_continuation(target)
    with pytest.raises(InvalidTaskTransition):
        task.transition_to(TaskState.PLANNING)
    assert task.rework_rounds == 2


def test_migration_14_is_additive_and_receipt_fields_are_strict(tmp_path):
    from app.storage.continuation_cancellations import ContinuationCancellationRepository
    from app.storage.continuations import ContinuationRepository
    claims = ContinuationRepository(SQLiteDatabase(tmp_path / "migration.sqlite3"))
    claims.initialize()
    ContinuationCancellationRepository(claims).initialize()
    authorizations = ContinuationAuthorizationRepository(claims)
    authorizations.initialize()
    with claims.database.connect() as connection:
        before = [tuple(row) for row in connection.execute("SELECT * FROM schema_migrations ORDER BY version")]
    repository = ContinuationResumptionRepository(authorizations)
    repository.initialize()
    repository.initialize()
    assert claims.database.schema_version == 14
    with claims.database.connect() as connection:
        assert [tuple(row) for row in connection.execute("SELECT * FROM schema_migrations WHERE version<14 ORDER BY version")] == before
    with pytest.raises(ValidationError):
        ContinuationResumptionReceipt.model_validate({"task_revision": True, "agent_dispatched": True})
