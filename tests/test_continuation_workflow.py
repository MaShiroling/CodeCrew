"""Offline Human-to-guard acceptance with Fake Agents and real Git/SQLite."""

import asyncio
import sqlite3
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from app.agents import FakeAgentScenario
from app.api.models import CreateTaskRequest
from app.orchestration.models import TaskState
from app.recovery import EvidenceRecoveryService
from app.trace import TraceEventType
from app.verification import VerificationCheckKind
from tests.test_continuation_execution import client, finished, granted
from tests.test_task_api_e2e import make_repository, make_runtime

pytest_plugins = ("tests.test_continuation_execution",)


@pytest_asyncio.fixture
async def waiting_for_planner(tmp_path):
    runtime, agents = make_runtime(tmp_path)
    agents[0]._scenario = FakeAgentScenario(output={"actions": [
        {"action": "request_human_input", "recipient": {"kind": "role", "role": "human"},
         "content": "Which value should be implemented?"},
        {"action": "finish_turn", "content": "Waiting for answer"},
    ]})
    repository = make_repository(tmp_path)
    created = await runtime.service.create_task(CreateTaskRequest(
        issue="Set value to two", repository_path=str(repository),
    ))
    await asyncio.wait_for(runtime.service.wait_for(created.task_id), timeout=10)
    view = await runtime.service.get_task(created.task_id)
    assert view.state is TaskState.NEEDS_HUMAN
    assert [len(agent.requests) for agent in agents] == [1, 0, 0]
    agents[0]._scenario = FakeAgentScenario(output={"actions": [
        {"action": "share_plan", "recipient": {"kind": "role", "role": "implementer"},
         "content": "Change value and verify", "artifact_content": {"steps": ["edit src/app.py"]}},
        {"action": "finish_turn", "content": "Plan ready"},
    ]})
    yield runtime.service, view, agents
    await runtime.service.shutdown()


async def human_command(api, view, *, role="planner"):
    posted = await api.post(f"/api/v1/tasks/{view.task_id}/messages", json={
        "expected_revision": view.revision,
        "idempotency_key": str(uuid4()),
        "recipient_role": role,
        "content": "Implement value 2 and run every configured check.",
    })
    assert posted.status_code == 201, posted.text
    return {
        "expected_revision": view.revision,
        "message_id": posted.json()["message"]["message_id"],
        "target_role": role,
        "idempotency_key": str(uuid4()),
    }


@pytest.mark.asyncio
async def test_human_reply_runs_fresh_verifier_reviewer_and_guard(waiting_for_planner, tmp_path):
    service, view, agents = waiting_for_planner
    async with client(service) as api:
        body = await human_command(api, view)
        accepted = await api.post(f"/api/v1/tasks/{view.task_id}/continue/workflow", json=body)
        assert accepted.status_code == 202, accepted.text
        request_id = accepted.json()["receipt"]["request"]["request_id"]
        assert accepted.json()["receipt"]["task_completion_evaluated"] is False
        assert (await finished(service, request_id)).receipt.state.value == "succeeded"
        outcome_response = await api.get(f"/api/v1/tasks/{view.task_id}/continuations/{request_id}/workflow")
        assert outcome_response.status_code == 200, outcome_response.text
        outcome = outcome_response.json()
        assert outcome["success"] is True
        assert outcome["final_state"] == "completed"
        assert outcome["completion_evaluated"] is True
        assert all(outcome[f"{part}_artifact_id"] for part in ("verification", "review", "completion"))
        assert len(outcome["agent_session_ids"]) == 3
        assert (await api.post(f"/api/v1/tasks/{view.task_id}/continue/workflow", json=body)).status_code == 202
        assert (await api.post(f"/api/v1/tasks/{view.task_id}/continue", json=body)).status_code == 409
    assert service.tasks.get(view.task_id).task.state is TaskState.COMPLETED
    evidence = EvidenceRecoveryService(service.router.artifacts, service.router.trace_store).recover(
        task_id=view.task_id, trace_id=view.trace_id,
    )
    assert evidence.verification is not None and evidence.verification.passed
    assert evidence.verification.change_set.diff_artifact is not None
    assert {
        VerificationCheckKind.STATIC_ANALYSIS,
        VerificationCheckKind.PUBLIC_TESTS,
        VerificationCheckKind.HIDDEN_TESTS,
        VerificationCheckKind.DIFF,
        VerificationCheckKind.PERMISSION,
        VerificationCheckKind.COMMAND_POLICY,
    } <= {check.kind for check in evidence.verification.checks}
    assert evidence.verification.permission_report.passed
    assert evidence.review is not None and evidence.review.verdict.value == "approved"
    assert evidence.completion is not None and evidence.completion.passed
    assert service.rooms.latest_plan_revision(service.contexts.get(view.task_id).context.room_id).version == 1
    assert service.rooms.get_message(UUID(body["message_id"])).deliveries[0].status.value == "acknowledged"
    assert [len(agent.requests) for agent in agents] == [2, 1, 1]
    traces = service.router.trace_store.list(trace_id=view.trace_id, limit=1000)
    kind = [entry.event.type for entry in traces]
    admission = kind.index(TraceEventType.CONTINUATION_EXECUTION_ACCEPTED)
    assert admission < kind.index(TraceEventType.VERIFICATION_COMPLETED)
    assert kind.index(TraceEventType.VERIFICATION_COMPLETED) < kind.index(TraceEventType.REVIEW_DECIDED)
    assert kind.index(TraceEventType.REVIEW_DECIDED) < kind.index(TraceEventType.COMPLETION_DECIDED)
    assert kind.index(TraceEventType.COMPLETION_DECIDED) < kind.index(TraceEventType.CONTINUATION_WORKFLOW_FINISHED)
    await service.shutdown()
    restarted, new_agents = make_runtime(tmp_path)
    try:
        report = await restarted.recovery.recover_startup()
        assert not report.resumed
        assert restarted.service.tasks.get(view.task_id).task.state is TaskState.COMPLETED
        assert restarted.service.continuations.workflow_outcome(
            task_id=view.task_id, request_id=UUID(request_id),
        ).success
        assert [len(agent.requests) for agent in new_agents] == [0, 0, 0]
    finally:
        await restarted.service.shutdown()


@pytest.mark.asyncio
async def test_agent_prose_cannot_complete_without_fresh_verification(waiting_for_planner):
    service, view, agents = waiting_for_planner
    agents[0]._scenario = FakeAgentScenario(output={"actions": [
        {"action": "report_progress", "recipient": {"kind": "role", "role": "orchestrator"},
         "content": "Everything is complete and tested"},
        {"action": "finish_turn", "content": "Done"},
    ]})
    async with client(service) as api:
        body = await human_command(api, view)
        accepted = await api.post(f"/api/v1/tasks/{view.task_id}/continue/workflow", json=body)
        assert accepted.status_code == 202
        request_id = accepted.json()["receipt"]["request"]["request_id"]
        await finished(service, request_id)
        outcome = (await api.get(f"/api/v1/tasks/{view.task_id}/continuations/{request_id}/workflow")).json()
        assert outcome["success"] is False
        assert outcome["completion_evaluated"] is False
        assert outcome["final_state"] == "needs_human"
    assert service.tasks.get(view.task_id).task.state is TaskState.NEEDS_HUMAN
    assert [len(agent.requests) for agent in agents] == [2, 0, 0]


@pytest.mark.asyncio
async def test_duplicate_workflow_admission_dispatches_once_and_reviewer_cannot_start(waiting_for_planner):
    service, view, agents = waiting_for_planner
    async with client(service) as api:
        body = await human_command(api, view)
        endpoint = f"/api/v1/tasks/{view.task_id}/continue/workflow"
        denied = await api.post(endpoint, json={**body, "target_role": "reviewer"})
        assert denied.status_code == 409
        assert service.continuations.active_for_task(view.task_id) is None
        replies = await asyncio.gather(*(api.post(endpoint, json=body) for _ in range(4)))
        assert all(reply.status_code == 202 for reply in replies)
        ids = {reply.json()["receipt"]["request"]["request_id"] for reply in replies}
        assert len(ids) == 1
        await finished(service, ids.pop())
    assert [len(agent.requests) for agent in agents] == [2, 1, 1]


@pytest.mark.asyncio
async def test_authorized_implementer_reply_runs_new_verification_and_review(production_paused):
    service, view, agents = production_paused
    agents[1]._scenario = FakeAgentScenario(output={"actions": [
        {"action": "request_review", "recipient": {"kind": "role", "role": "orchestrator"},
         "content": "Ready for a new verification"},
        {"action": "finish_turn", "content": "Implementation ready"},
    ]})
    agents[2]._scenario = FakeAgentScenario(output={"actions": [
        {"action": "approve_review", "recipient": {"kind": "role", "role": "orchestrator"},
         "content": "Approved", "artifact_content": {"issues": []}},
        {"action": "finish_turn", "content": "Review complete"},
    ]})
    body = await granted(production_paused, "implementer")
    with service.tasks.database.connect() as connection:
        prior_count = connection.execute(
            "SELECT COUNT(*) AS n FROM trace_events WHERE trace_id=? AND event_type=?",
            (str(view.trace_id), TraceEventType.VERIFICATION_COMPLETED.value),
        ).fetchone()["n"]
    async with client(service) as api:
        accepted = await api.post(f"/api/v1/tasks/{view.task_id}/continue/workflow", json=body)
        assert accepted.status_code == 202, accepted.text
        request_id = accepted.json()["receipt"]["request"]["request_id"]
        await finished(service, request_id)
        result = (await api.get(f"/api/v1/tasks/{view.task_id}/continuations/{request_id}/workflow")).json()
        assert result["success"] is True
        assert len(result["agent_session_ids"]) == 2
    assert service.tasks.get(view.task_id).task.state is TaskState.COMPLETED
    with service.tasks.database.connect() as connection:
        current_count = connection.execute(
            "SELECT COUNT(*) AS n FROM trace_events WHERE trace_id=? AND event_type=?",
            (str(view.trace_id), TraceEventType.VERIFICATION_COMPLETED.value),
        ).fetchone()["n"]
    assert current_count == prior_count + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_role", ["planner", "reviewer"])
async def test_failed_agent_never_commits_task_or_selected_ack(waiting_for_planner, failed_role):
    service, view, agents = waiting_for_planner
    target = agents[0] if failed_role == "planner" else agents[2]
    target._scenario = FakeAgentScenario(start_error="unavailable")
    async with client(service) as api:
        body = await human_command(api, view)
        accepted = await api.post(f"/api/v1/tasks/{view.task_id}/continue/workflow", json=body)
        assert accepted.status_code == 202
        request_id = accepted.json()["receipt"]["request"]["request_id"]
        receipt = (await finished(service, request_id)).receipt
        assert receipt.state.value == "needs_human"
        assert receipt.failure_code == "execution_failed"
        assert (await api.get(f"/api/v1/tasks/{view.task_id}/continuations/{request_id}/workflow")).status_code == 404
        assert (await api.post(f"/api/v1/tasks/{view.task_id}/continue/workflow", json=body)).json()["receipt"]["state"] == "needs_human"
    assert service.tasks.get(view.task_id).task.state is TaskState.NEEDS_HUMAN
    assert service.rooms.get_message(UUID(body["message_id"])).deliveries[0].status.value == "pending"


@pytest.mark.asyncio
async def test_workflow_cancel_and_restart_do_not_dispatch_again(waiting_for_planner, tmp_path):
    service, view, agents = waiting_for_planner
    agents[0]._scenario = FakeAgentScenario(block_until_cancel=True)
    async with client(service) as api:
        body = await human_command(api, view)
        accepted = await api.post(f"/api/v1/tasks/{view.task_id}/continue/workflow", json=body)
        assert accepted.status_code == 202
        request_id = accepted.json()["receipt"]["request"]["request_id"]
        claim = service.continuations.get(UUID(request_id))
        other, _ = make_runtime(tmp_path)
        try:
            async with client(other.service) as second_api:
                concurrent_message = await second_api.post(
                    f"/api/v1/tasks/{view.task_id}/messages", json={
                        "expected_revision": view.revision,
                        "idempotency_key": str(uuid4()),
                        "recipient_role": "planner",
                        "content": "Must not append while another service owns the workflow",
                    },
                )
                assert concurrent_message.status_code == 409
        finally:
            await other.service.shutdown()
        current = (await api.get(f"/api/v1/tasks/{view.task_id}/continuations/{request_id}")).json()
        cancelled = await api.post(f"/api/v1/tasks/{view.task_id}/continuations/{request_id}/cancel", json={
            "idempotency_key": str(uuid4()),
            "expected_revision": current["task_revision"],
            "expected_runtime_revision": current["runtime_revision"],
            "expected_claim_updated_at": claim.updated_at.isoformat(),
            "reason": "Stop the controlled workflow",
        })
        assert cancelled.status_code == 202, cancelled.text
        receipt = (await finished(service, request_id)).receipt
        assert receipt.failure_code == "cancelled"
        assert (await api.get(f"/api/v1/tasks/{view.task_id}/continuations/{request_id}/workflow")).status_code == 404
    before = [len(agent.requests) for agent in agents]
    await service.shutdown()
    restarted, new_agents = make_runtime(tmp_path)
    try:
        report = await restarted.recovery.recover_startup()
        assert not report.resumed
        assert restarted.service.tasks.get(view.task_id).task.state is TaskState.NEEDS_HUMAN
        assert restarted.service.continuations.workflow_outcome(
            task_id=view.task_id, request_id=UUID(request_id),
        ) is None
        assert [len(agent.requests) for agent in new_agents] == [0, 0, 0]
        assert [len(agent.requests) for agent in agents] == before
    finally:
        await restarted.service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("approve_after_one", [True, False])
async def test_rework_is_bounded_and_never_self_approves(waiting_for_planner, approve_after_one):
    service, view, agents = waiting_for_planner
    issue_id = str(uuid4())
    reject = FakeAgentScenario(output={"actions": [
        {"action": "request_rework", "recipient": {"kind": "role", "role": "orchestrator"},
         "content": "Evidence needs correction", "artifact_content": {"issues": [
             {"issue_id": issue_id, "priority": "high",
              "summary": "Please correct this", "resolved": False},
         ]}},
        {"action": "finish_turn", "content": "Rework requested"},
    ]})
    approve = FakeAgentScenario(output={"actions": [
        {"action": "approve_review", "recipient": {"kind": "role", "role": "orchestrator"},
         "content": "Corrected and approved", "artifact_content": {"issues": [
             {"issue_id": issue_id, "priority": "high",
              "summary": "Please correct this", "resolved": True},
         ]}},
        {"action": "finish_turn", "content": "Review approved"},
    ]})
    agents[2]._scenario = reject
    if approve_after_one:
        original_wait = agents[2].wait

        async def switch_after_first(session_id):
            result = await original_wait(session_id)
            if len(agents[2].requests) == 1:
                agents[2]._scenario = approve
            return result

        agents[2].wait = switch_after_first
    async with client(service) as api:
        body = await human_command(api, view)
        accepted = await api.post(f"/api/v1/tasks/{view.task_id}/continue/workflow", json=body)
        assert accepted.status_code == 202, accepted.text
        request_id = accepted.json()["receipt"]["request"]["request_id"]
        worker = service._continuation_runs[UUID(request_id)][1]
        record = await finished(service, request_id)
        assert record.receipt.state.value == "succeeded", repr(worker.exception())
        response = await api.get(f"/api/v1/tasks/{view.task_id}/continuations/{request_id}/workflow")
        assert response.status_code == 200, response.text
        outcome = response.json()
    assert outcome["success"] is approve_after_one
    assert outcome["rework_rounds"] == (1 if approve_after_one else 2)
    assert outcome["final_state"] == ("completed" if approve_after_one else "needs_human")
    assert len(agents[1].requests) == (2 if approve_after_one else 3)
    assert len(agents[2].requests) == (2 if approve_after_one else 3)
    assert service.tasks.get(view.task_id).task.state is (
        TaskState.COMPLETED if approve_after_one else TaskState.NEEDS_HUMAN
    )


@pytest.mark.asyncio
async def test_cancel_while_reviewer_is_running_fences_success(waiting_for_planner):
    service, view, agents = waiting_for_planner
    agents[2]._scenario = FakeAgentScenario(block_until_cancel=True)
    async with client(service) as api:
        body = await human_command(api, view)
        accepted = await api.post(f"/api/v1/tasks/{view.task_id}/continue/workflow", json=body)
        assert accepted.status_code == 202
        request_id = accepted.json()["receipt"]["request"]["request_id"]
        for _ in range(500):
            if agents[2].requests:
                break
            await asyncio.sleep(0.01)
        assert agents[2].requests
        current = (await api.get(f"/api/v1/tasks/{view.task_id}/continuations/{request_id}")).json()
        cancelled = await api.post(f"/api/v1/tasks/{view.task_id}/continuations/{request_id}/cancel", json={
            "idempotency_key": str(uuid4()),
            "expected_revision": current["task_revision"],
            "expected_runtime_revision": current["runtime_revision"],
            "expected_claim_updated_at": current["updated_at"],
            "reason": "Stop downstream Reviewer",
        })
        assert cancelled.status_code == 202, cancelled.text
        receipt = (await finished(service, request_id)).receipt
        assert receipt.failure_code == "cancelled"
        assert (await api.get(f"/api/v1/tasks/{view.task_id}/continuations/{request_id}/workflow")).status_code == 404
    assert service.tasks.get(view.task_id).task.state is TaskState.NEEDS_HUMAN
    assert service.rooms.get_message(UUID(body["message_id"])).deliveries[0].status.value == "pending"


@pytest.mark.asyncio
async def test_final_trace_failure_rolls_back_task_ack_and_result(waiting_for_planner, monkeypatch):
    service, view, _ = waiting_for_planner
    append = service.continuations.traces.append_in_transaction

    def fail_final_trace(connection, event):
        if event.type is TraceEventType.CONTINUATION_WORKFLOW_FINISHED:
            raise sqlite3.OperationalError("injected final trace fault")
        return append(connection, event)

    monkeypatch.setattr(service.continuations.traces, "append_in_transaction", fail_final_trace)
    async with client(service) as api:
        body = await human_command(api, view)
        accepted = await api.post(f"/api/v1/tasks/{view.task_id}/continue/workflow", json=body)
        assert accepted.status_code == 202
        request_id = accepted.json()["receipt"]["request"]["request_id"]
        record = await finished(service, request_id)
        assert record.receipt.state.value == "needs_human"
        assert record.receipt.failure_code == "execution_failed"
        assert (await api.get(f"/api/v1/tasks/{view.task_id}/continuations/{request_id}/workflow")).status_code == 404
    assert service.tasks.get(view.task_id).task.state is TaskState.NEEDS_HUMAN
    assert service.rooms.get_message(UUID(body["message_id"])).deliveries[0].status.value == "pending"
