"""Offline single-target follow-ups on real SQLite/Git/Verifier evidence."""

import asyncio
import subprocess
from dataclasses import replace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app.agents import AgentAdapterError, AgentExitReason, FakeAgentScenario, PermissionMode
from app.api.continuation_runtime import HumanContinuationKernel
from app.api.models import ContinueTaskPreflightRequest, CreateTaskRequest, PostHumanMessageRequest
from app.api.service import TaskDetailUnavailable, TaskStateConflict
from app.main import create_app
from app.orchestration.models import TaskState
from app.storage import ArtifactIntegrityError
from app.team import MemberRole
from app.team.actions import ChatActionError
from app.team.budgets import ConversationBudgetPolicy
from app.team.turns import AgentTurnError
from app.trace import TraceEventType
from app.workspace import WorktreeError
from tests.test_task_api_e2e import make_repository, make_runtime


@pytest_asyncio.fixture
async def paused(tmp_path):
    runtime, agents = make_runtime(tmp_path)
    # Park the actual event loop after real verification, before any approval.
    agents[2]._scenario = FakeAgentScenario(output={"actions": [
        {"action": "request_human_input", "recipient": {"kind": "role", "role": "human"},
         "content": "Please clarify the compatibility requirement"},
        {"action": "finish_turn", "content": "Waiting for Human"},
    ]})
    service = runtime.service
    run = service.event_loop.run

    async def park_after_human_request(workflow, initial_events):
        result = await run(workflow, initial_events)
        assert result.paused
        workflow.task.transition_to(TaskState.NEEDS_HUMAN)
        return result.model_copy(update={"task": workflow.task})

    # Explicit test harness pause; the current normal chat wait does not itself
    # persist needs_human. Do not claim this is a new production pause path.
    service.event_loop.run = park_after_human_request
    repository = make_repository(tmp_path)
    (repository / "verify.py").write_text("from src.app import value\nassert value == 2\nprint('verified')\n")
    for args in (("add", "verify.py"), ("-c", "user.name=CodeCrew Tests", "-c",
                "user.email=tests@codecrew.invalid", "commit", "-m", "emit verification log")):
        await asyncio.to_thread(subprocess.run, ["git", *args], cwd=repository, check=True, capture_output=True)
    created = await service.create_task(CreateTaskRequest(
        issue="Set value to two", repository_path=str(repository),
    ))
    await asyncio.wait_for(service.wait_for(created.task_id), timeout=10)
    view = await service.get_task(created.task_id)
    assert view.state is TaskState.NEEDS_HUMAN
    assert [len(a.requests) for a in agents] == [1, 1, 1]
    context = service.contexts.get(view.task_id)
    # All controlled follow-ups must ignore these persisted native sessions.
    bindings = tuple(b.model_copy(update={"native_session_id": f"stale-{b.role.value}"})
                     for b in context.context.agent_bindings)
    service.contexts.save(context.context.model_copy(update={"agent_bindings": bindings}),
                          expected_revision=context.revision)
    # Subsequent turns emit chat only; no downstream Agent may run implicitly.
    for agent in agents:
        agent._scenario = FakeAgentScenario(output={"actions": [
            {"action": "report_progress", "recipient": {"kind": "role", "role": "orchestrator"},
             "content": "Human follow-up processed"},
            {"action": "finish_turn", "content": "Stopped after one turn"},
        ]})
    return service, view, agents


async def intent(paused, *, recipient="planner", target=None):
    service, view, _ = paused
    receipt = await service.post_human_message(view.task_id, PostHumanMessageRequest(
        content="Keep backward compatibility", expected_revision=view.revision,
        idempotency_key=uuid4(), recipient_role=recipient,
    ))
    request = ContinueTaskPreflightRequest(
        expected_revision=view.revision, message_id=receipt.message.message_id,
        target_role=target or ("planner" if recipient == "orchestrator" else recipient),
    )
    return HumanContinuationKernel(service), request


def state(service, view):
    context = service.contexts.get(view.task_id)
    return (service.tasks.get(view.task_id), context,
            service.rooms.list_messages(context.context.room_id),
            service.router.trace_store.list(trace_id=view.trace_id, limit=1000))


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["planner", "implementer", "reviewer"])
async def test_prepare_is_read_only_and_restores_scoped_evidence(paused, role):
    service, view, agents = paused
    kernel, request = await intent(paused, recipient=role)
    before = state(service, view)
    prepared = await kernel.prepare(view.task_id, request)
    assert state(service, view) == before
    assert prepared.runtime.task.state is TaskState.NEEDS_HUMAN
    assert prepared.runtime.latest_completion is None
    assert MemberRole(role) not in prepared.runtime.native_session_ids
    assert prepared.evidence.verification is not None
    assert prepared.evidence.verification.passed
    assert bool(prepared.runtime.latest_verification) == (role != "implementer")
    types = {r.type.value for r in prepared.references}
    assert {"plan", "diff", "verification_report", "command_audit", "test_log"} <= types
    assert [len(a.requests) for a in agents] == [1, 1, 1]
    assert (await kernel.prepare(view.task_id, request)).references == prepared.references


@pytest.mark.asyncio
@pytest.mark.parametrize("recipient,target,index", [
    ("planner", "planner", 0), ("implementer", "implementer", 1),
    ("reviewer", "reviewer", 2), ("orchestrator", "planner", 0),
    ("orchestrator", "implementer", 1), ("orchestrator", "reviewer", 2),
])
async def test_single_target_dispatch_is_fresh_scoped_and_leaves_task_parked(paused, recipient, target, index):
    service, view, agents = paused
    kernel, request = await intent(paused, recipient=recipient, target=target)
    _, unrelated = await intent(paused, recipient=target)
    before = state(service, view)
    executor = service.event_loop.executor
    usage_before = executor.budget_guard.usage(view.task_id, room_id=before[1].context.room_id)
    result = await kernel.run_single(view.task_id, request)
    assert [len(a.requests) for a in agents] == [2 if i == index else 1 for i in range(3)]
    turn_request = agents[index].requests[-1]
    assert turn_request.resume_from_session_id is None
    assert turn_request.permission_mode is (
        PermissionMode.WORKSPACE_WRITE if target == "implementer" else PermissionMode.READ_ONLY
    )
    assert turn_request.trace_id == view.trace_id
    assert {item.artifact_id for item in turn_request.artifact_inputs} == {
        ref.artifact_id for ref in result.prepared.references
    }
    assert len(result.result.agent_turns) == 1
    assert result.result.produced_events  # Buffered, not handled by the event loop.
    assert service.tasks.get(view.task_id) == before[0]
    assert result.runtime_revision == before[1].revision + 1
    assert service.rooms.get_message(unrelated.message_id).deliveries[0].status.value == "pending"
    assert service.rooms.get_message(request.message_id).deliveries[0].status.value == "acknowledged"
    assert str(unrelated.message_id) not in turn_request.metadata["input_message_ids"]
    after = state(service, view)
    assert len([t for t in after[3] if t.event.type is TraceEventType.VERIFICATION_COMPLETED]) == len([
        t for t in before[3] if t.event.type is TraceEventType.VERIFICATION_COMPLETED
    ])
    assert not any(t.event.type is TraceEventType.COMPLETION_DECIDED for t in after[3])
    usage = executor.budget_guard.usage(view.task_id, room_id=before[1].context.room_id)
    assert usage.agent_turns == usage_before.agent_turns + 1
    assert usage.turns_without_token_usage == usage_before.turns_without_token_usage + 1
    replay = await kernel.run_single(view.task_id, request)
    assert replay.replayed and replay.receipt == result.receipt
    assert replay.prepared is None and replay.result is None
    assert state(service, view) == after


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing_worktree", "branch", "context_worktree", "policy", "registry", "plan_blob", "log_blob"])
async def test_preparation_faults_do_not_dispatch_ack_or_write(paused, fault):
    service, view, agents = paused
    kernel, request = await intent(paused)
    context = service.contexts.get(view.task_id)
    if fault == "missing_worktree":
        service.worktrees._manifest_path(view.task_id).unlink()
    elif fault == "branch":
        await asyncio.to_thread(subprocess.run, ["git", "checkout", "-b", "unexpected"],
                                cwd=context.context.worktree.worktree_path, check=True, capture_output=True)
    elif fault == "context_worktree":
        handle = context.context.worktree.model_copy(update={"base_revision": "0" * 40})
        service.worktrees._write_manifest(handle)
    elif fault == "policy":
        service.verification_plan = service.verification_plan.model_copy(update={"commands": ()})
    elif fault == "registry":
        service.event_loop.executor.turns.registry.unregister(agents[0].name)
    else:
        prepared = await kernel.prepare(view.task_id, request)
        reference = next(r for r in prepared.references if r.type.value == ("plan" if fault == "plan_blob" else "test_log"))
        service.router.artifacts.blob_path_for(reference.artifact_id).write_text("corrupted")
    before = state(service, view)
    from app.agents.registry import AgentRegistryError
    from app.recovery import EvidenceRecoveryError
    with pytest.raises((WorktreeError, TaskDetailUnavailable, AgentRegistryError, ArtifactIntegrityError, EvidenceRecoveryError)):
        await kernel.run_single(view.task_id, request)
    assert state(service, view) == before
    assert [len(a.requests) for a in agents] == [1, 1, 1]


@pytest.mark.asyncio
async def test_revision_change_during_git_inspection_invalidates_preparation(paused, monkeypatch):
    service, view, agents = paused
    kernel, request = await intent(paused)
    original = service.worktrees.inspect

    async def changed(task_id):
        handle = await original(task_id)
        context = service.contexts.get(task_id)
        service.contexts.save(context.context, expected_revision=context.revision)
        return handle

    monkeypatch.setattr(service.worktrees, "inspect", changed)
    with pytest.raises(TaskStateConflict, match="changed during"):
        await kernel.run_single(view.task_id, request)
    assert service.rooms.get_message(request.message_id).deliveries[0].status.value == "pending"
    assert [len(a.requests) for a in agents] == [1, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["exit", "output", "start"])
async def test_failed_turn_preserves_human_intent_and_pause_without_retry(paused, fault):
    service, view, agents = paused
    kernel, request = await intent(paused, recipient="orchestrator")
    if fault == "exit":
        agents[0]._scenario = replace(agents[0]._scenario, reason=AgentExitReason.FAILED, exit_code=1)
    elif fault == "output":
        agents[0]._scenario = replace(agents[0]._scenario, output={"not_actions": []})
    else:
        agents[0]._scenario = replace(agents[0]._scenario, start_error="unavailable")
    before = state(service, view)
    with pytest.raises((AgentTurnError, AgentAdapterError, ChatActionError)):
        await kernel.run_single(view.task_id, request)
    assert service.tasks.get(view.task_id) == before[0]
    assert service.contexts.get(view.task_id) == before[1]
    assert service.rooms.get_message(request.message_id).deliveries[0].status.value == "pending"
    assert [len(a.requests) for a in agents] == ([1, 1, 1] if fault == "start" else [2, 1, 1])
    events = service.router.trace_store.list(trace_id=view.trace_id, limit=1000)
    assert any(t.event.type is TraceEventType.AGENT_TURN_FAILED for t in events)
    assert not any(t.event.type is TraceEventType.COMPLETION_DECIDED for t in events)


@pytest.mark.asyncio
async def test_handoff_message_budget_is_checked_before_writes(paused):
    service, view, agents = paused
    kernel, request = await intent(paused)
    guard = service.event_loop.executor.budget_guard
    usage = guard.usage(view.task_id, room_id=service.contexts.get(view.task_id).context.room_id)
    guard.policy = ConversationBudgetPolicy(max_room_messages=usage.room_messages + 1)
    before = state(service, view)
    with pytest.raises(TaskStateConflict, match="handoff"):
        await kernel.run_single(view.task_id, request)
    assert state(service, view) == before
    assert [len(a.requests) for a in agents] == [1, 1, 1]


@pytest.mark.asyncio
async def test_local_parallel_attempts_only_start_one_turn_and_http_stays_absent(paused):
    service, view, agents = paused
    kernel, request = await intent(paused)
    outcomes = await asyncio.gather(*(kernel.run_single(view.task_id, request) for _ in range(2)), return_exceptions=True)
    assert sum(result.replayed for result in outcomes) == 1
    assert outcomes[0].receipt == outcomes[1].receipt
    assert [len(a.requests) for a in agents] == [2, 1, 1]
    client = TestClient(create_app(task_service=service))
    assert client.post(f"/api/v1/tasks/{view.task_id}/continue", json=request.model_dump(mode="json")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["approve_review", "request_rework"])
async def test_historical_evidence_cannot_authorize_reviewer_decisions(paused, verdict):
    service, view, agents = paused
    kernel, request = await intent(paused, recipient="reviewer")
    issues = [] if verdict == "approve_review" else [{
        "issue_id": str(uuid4()), "priority": "high", "summary": "Needs current tests", "resolved": False,
    }]
    agents[2]._scenario = FakeAgentScenario(output={"actions": [
        {"action": verdict, "recipient": {"kind": "role", "role": "orchestrator"},
         "content": "Historical verdict", "artifact_content": {"issues": issues}},
        {"action": "finish_turn"},
    ]})
    before = state(service, view)
    with pytest.raises(AgentTurnError, match="historical evidence"):
        await kernel.run_single(view.task_id, request)
    assert service.rooms.get_message(request.message_id).deliveries[0].status.value == "pending"
    assert service.tasks.get(view.task_id) == before[0]
    assert not any(t.event.type is TraceEventType.REVIEW_DECIDED
                   for t in service.router.trace_store.list(trace_id=view.trace_id, limit=1000))


@pytest.mark.asyncio
async def test_max_length_orchestrator_intent_is_preserved_without_copying_history(paused):
    service, view, agents = paused
    receipt = await service.post_human_message(view.task_id, PostHumanMessageRequest(
        content="x" * 16000, expected_revision=view.revision, idempotency_key=uuid4(), recipient_role="orchestrator",
    ))
    await HumanContinuationKernel(service).run_single(view.task_id, ContinueTaskPreflightRequest(
        expected_revision=view.revision, message_id=receipt.message.message_id, target_role="planner",
    ))
    assert "x" * 16000 in agents[0].requests[-1].prompt


@pytest.mark.asyncio
async def test_cancelled_turn_leaves_intent_pending_and_releases_local_lock(paused, monkeypatch):
    service, view, agents = paused
    kernel, request = await intent(paused)
    agents[0]._scenario = replace(agents[0]._scenario, block_until_cancel=True)
    started = asyncio.Event()
    start = agents[0].start

    async def signal(request):
        session = await start(request)
        started.set()
        return session

    monkeypatch.setattr(agents[0], "start", signal)
    before = state(service, view)
    operation = asyncio.create_task(kernel.run_single(view.task_id, request))
    await asyncio.wait_for(started.wait(), timeout=2)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert service.tasks.get(view.task_id) == before[0]
    assert service.contexts.get(view.task_id) == before[1]
    assert service.rooms.get_message(request.message_id).deliveries[0].status.value == "pending"
    assert not service._lock.locked()
    assert [len(a.requests) for a in agents] == [2, 1, 1]
