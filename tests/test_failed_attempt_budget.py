"""Offline accounting: charge attempted dispatch, never invent Token costs."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from app.agents import AgentAdapterError, AgentExitReason, TokenUsage
from app.api.service import TaskStateConflict
from app.orchestration.models import utc_now
from app.team.actions import ChatActionError
from app.team.budgets import ConversationBudgetGuard
from app.team.turns import AgentAttemptUsage, AgentTurnError
from tests import test_continuation_runtime as runtime_tests
from tests.test_conversation_budget import make_context, make_turn, permissive_policy

paused = runtime_tests.paused


def budget(paused):
    service, view, _ = paused
    guard = service.event_loop.executor.budget_guard
    room_id = service.contexts.get(view.task_id).context.room_id
    return guard, room_id, guard.usage(view.task_id, room_id=room_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["start", "failed", "timed_out", "output", "audit"])
async def test_failed_dispatch_is_charged_once_and_replay_is_free(paused, fault):
    service, view, agents = paused
    kernel, request = await runtime_tests.intent(paused, recipient="reviewer" if fault == "audit" else "planner")
    agent = agents[2] if fault == "audit" else agents[0]
    tokens = TokenUsage(input_tokens=11, output_tokens=7)
    scenario = replace(agent._scenario, token_usage=tokens)
    if fault == "start":
        scenario = replace(scenario, start_error="startup failed")
    elif fault in {"failed", "timed_out"}:
        scenario = replace(scenario, reason=AgentExitReason(fault), exit_code=1)
    elif fault == "output":
        scenario = replace(scenario, output={"not_actions": []})
    else:
        scenario = replace(scenario, output={"actions": [
            {"action": "approve_review", "recipient": {"kind": "role", "role": "orchestrator"},
             "artifact_content": {"issues": []}, "content": "Historical approval"},
            {"action": "finish_turn"},
        ]})
    agent._scenario = scenario
    guard, room_id, before = budget(paused)
    with pytest.raises((AgentTurnError, AgentAdapterError, ChatActionError)):
        await kernel.run_single(view.task_id, request)
    after = guard.usage(view.task_id, room_id=room_id)
    assert after.agent_turns == before.agent_turns + 1
    assert after.reported_total_tokens == before.reported_total_tokens + (0 if fault == "start" else 18)
    assert after.turns_without_token_usage == before.turns_without_token_usage + (fault == "start")
    replay = await kernel.run_single(view.task_id, request)
    assert replay.replayed and replay.receipt.state.value == "needs_human"
    assert guard.usage(view.task_id, room_id=room_id) == after
    assert service.rooms.get_message(request.message_id).deliveries[0].status.value == "pending"


@pytest.mark.asyncio
async def test_reservation_exists_while_start_is_in_flight_and_cancel_keeps_it(paused, monkeypatch):
    _, view, agents = paused
    kernel, request = await runtime_tests.intent(paused)
    guard, room_id, before = budget(paused)
    started = asyncio.Event()

    async def blocked_start(request):
        usage = guard.usage(view.task_id, room_id=room_id)
        assert usage.agent_turns == before.agent_turns + 1
        assert usage.turns_without_token_usage == before.turns_without_token_usage + 1
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(agents[0], "start", blocked_start)
    operation = asyncio.create_task(kernel.run_single(view.task_id, request))
    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.sleep(0.02)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    usage = guard.usage(view.task_id, room_id=room_id)
    assert usage.agent_turns == before.agent_turns + 1
    assert usage.agent_duration_ms >= before.agent_duration_ms + 10
    assert usage.turns_without_token_usage == before.turns_without_token_usage + 1
    assert (await kernel.run_single(view.task_id, request)).receipt.failure_code == "cancelled"


@pytest.mark.asyncio
async def test_reservation_failure_prevents_model_start(paused, monkeypatch):
    _, view, agents = paused
    kernel, request = await runtime_tests.intent(paused)
    guard, _, _ = budget(paused)

    def fail(*args, **kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(guard, "record_attempt", fail)
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await kernel.run_single(view.task_id, request)
    assert [len(agent.requests) for agent in agents] == [1, 1, 1]
    assert (await kernel.run_single(view.task_id, request)).receipt.failure_code == "execution_failed"


@pytest.mark.asyncio
async def test_cancelled_running_session_retains_unknown_cost_and_elapsed_time(paused, monkeypatch):
    service, view, agents = paused
    kernel, request = await runtime_tests.intent(paused)
    guard, room_id, before = budget(paused)
    agents[0]._scenario = replace(agents[0]._scenario, block_until_cancel=True)
    started = asyncio.Event()
    start = agents[0].start

    async def signal(request):
        session = await start(request)
        started.set()
        return session

    monkeypatch.setattr(agents[0], "start", signal)
    operation = asyncio.create_task(kernel.run_single(view.task_id, request))
    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.sleep(0.02)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    after = guard.usage(view.task_id, room_id=room_id)
    assert after.agent_turns == before.agent_turns + 1
    assert after.agent_duration_ms >= before.agent_duration_ms + 10
    assert after.turns_without_token_usage == before.turns_without_token_usage + 1
    assert service.tasks.get(view.task_id).task.rework_rounds == view.rework_rounds


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["stream", "wrong_result", "final_write", "final_write_in_handler"])
async def test_interrupted_accounting_preserves_reservation(paused, monkeypatch, fault):
    _, view, agents = paused
    kernel, request = await runtime_tests.intent(paused)
    guard, room_id, before = budget(paused)
    if fault == "stream":
        async def broken_stream(session_id):
            raise RuntimeError("stream failed")
            yield  # pragma: no cover - async generator interface
        monkeypatch.setattr(agents[0], "stream", broken_stream)
    elif fault == "wrong_result":
        wait = agents[0].wait

        async def mismatched(session_id):
            result = await wait(session_id)
            return result.model_copy(update={"session_id": uuid4(),
                                             "token_usage": TokenUsage(input_tokens=99, output_tokens=99)})
        monkeypatch.setattr(agents[0], "wait", mismatched)
    else:
        record = guard.record_attempt

        def failed_final(task, attempt, **kwargs):
            if attempt.session is not None:
                raise RuntimeError("final accounting failed")
            return record(task, attempt, **kwargs)
        monkeypatch.setattr(guard, "record_attempt", failed_final)
    if fault == "final_write_in_handler":
        try:
            raise ValueError("unrelated caller failure")
        except ValueError:
            with pytest.raises(RuntimeError, match="final accounting failed"):
                await kernel.run_single(view.task_id, request)
    else:
        with pytest.raises((RuntimeError, AgentTurnError)):
            await kernel.run_single(view.task_id, request)
    after = guard.usage(view.task_id, room_id=room_id)
    assert after.agent_turns == before.agent_turns + 1
    assert after.turns_without_token_usage == before.turns_without_token_usage + 1
    assert after.reported_total_tokens == before.reported_total_tokens
    assert (await kernel.run_single(view.task_id, request)).replayed


@pytest.mark.asyncio
async def test_failed_attempt_exhausts_existing_budget_without_reset(paused):
    _, view, agents = paused
    kernel, request = await runtime_tests.intent(paused)
    guard, room_id, before = budget(paused)
    guard.policy = guard.policy.model_copy(update={"max_agent_turns": before.agent_turns + 1})
    agents[0]._scenario = replace(agents[0]._scenario, start_error="unavailable")
    with pytest.raises(AgentAdapterError):
        await kernel.run_single(view.task_id, request)
    assert guard.evaluate(view.task_id, room_id=room_id).code.value == "agent_turns"
    with pytest.raises(TaskStateConflict):
        await kernel.prepare(view.task_id, request)
    assert guard.usage(view.task_id, room_id=room_id).agent_turns == before.agent_turns + 1
    assert [len(agent.requests) for agent in agents] == [1, 1, 1]


def test_reserved_unknown_survives_reopen_and_cannot_erase_known_facts(tmp_path):
    rooms, task, room, planner, _ = make_context(tmp_path)
    guard = ConversationBudgetGuard(rooms, permissive_policy(max_agent_turns=1))
    guard.initialize()
    attempt = AgentAttemptUsage(uuid4(), utc_now())
    kwargs = {"task": task, "room_id": room.room_id, "member_id": planner.member_id, "agent_name": "fake-planner"}
    guard.record_attempt(**kwargs, attempt=attempt)
    reopened = ConversationBudgetGuard(rooms, guard.policy)
    assert reopened.usage(task.id, room_id=room.room_id).turns_without_token_usage == 1
    assert reopened.evaluate(task.id, room_id=room.room_id).code.value == "agent_turns"
    turn = make_turn(task)
    finished = replace(attempt, session=turn.session, result=turn.agent_result, duration_ms=1000)
    guard.record_attempt(**kwargs, attempt=finished)
    guard.record_attempt(**kwargs, attempt=finished)
    guard.record_attempt(**kwargs, attempt=attempt)
    usage = guard.usage(task.id, room_id=room.room_id)
    assert usage.agent_turns == 1 and usage.reported_total_tokens == 30
    assert usage.agent_duration_ms == 1000 and usage.turns_without_token_usage == 0


@pytest.mark.parametrize("fault", ["identity", "tokens", "result_session", "session_trace"])
def test_conflicting_accounting_facts_fail_closed(tmp_path, fault):
    rooms, task, room, planner, _ = make_context(tmp_path)
    guard = ConversationBudgetGuard(rooms)
    guard.initialize()
    turn = make_turn(task)
    kwargs = {"task": task, "room_id": room.room_id, "member_id": planner.member_id, "agent_name": "fake-planner"}
    attempt = AgentAttemptUsage(uuid4(), utc_now(), 250, turn.session, turn.agent_result)
    guard.record_attempt(**kwargs, attempt=attempt)
    before = guard.usage(task.id, room_id=room.room_id)
    if fault == "identity":
        attempt = replace(attempt, started_at=utc_now())
    elif fault == "tokens":
        attempt = replace(attempt, result=turn.agent_result.model_copy(update={
            "token_usage": TokenUsage(input_tokens=99, output_tokens=10),
        }))
    elif fault == "result_session":
        attempt = replace(attempt, result=turn.agent_result.model_copy(update={"session_id": uuid4()}))
    else:
        attempt = replace(attempt, session=turn.session.model_copy(update={"trace_id": uuid4()}))
    with pytest.raises(ValueError):
        guard.record_attempt(**kwargs, attempt=attempt)
    assert guard.usage(task.id, room_id=room.room_id) == before
