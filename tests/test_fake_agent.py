from pathlib import Path
from uuid import uuid4

import pytest

from app.agents import (
    AgentAdapterError,
    AgentEventType,
    AgentExitReason,
    AgentRequest,
    AgentRole,
    AgentSessionNotFoundError,
    AgentSessionStatus,
    FakeAgentAdapter,
    FakeAgentScenario,
    FakeEventSpec,
    TokenUsage,
)


def make_request() -> AgentRequest:
    return AgentRequest(
        task_id=uuid4(),
        trace_id=uuid4(),
        role=AgentRole.PLANNER,
        prompt="Create a structured plan.",
        working_directory=Path("/tmp/repository"),
    )


@pytest.mark.asyncio
async def test_fake_adapter_emits_normalized_success_lifecycle() -> None:
    adapter = FakeAgentAdapter(
        FakeAgentScenario(
            events=(FakeEventSpec(text="plan ready", data={"artifact_id": "plan-1"}),),
            output={"artifact_id": "plan-1"},
            token_usage=TokenUsage(input_tokens=10, output_tokens=5),
        )
    )

    request = make_request()
    session = await adapter.start(request)
    events = [event async for event in adapter.stream(session.session_id)]
    result = await adapter.wait(session.session_id)

    assert [event.type for event in events] == [
        AgentEventType.STARTED,
        AgentEventType.MESSAGE,
        AgentEventType.COMPLETED,
    ]
    assert [event.sequence for event in events] == [0, 1, 2]
    assert result.reason is AgentExitReason.COMPLETED
    assert result.output == {"artifact_id": "plan-1"}
    assert result.token_usage is not None
    assert result.token_usage.total_tokens == 15
    assert session.status is AgentSessionStatus.COMPLETED
    assert adapter.requests == [request]
    assert adapter.requests[0] is not request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected_status"),
    [
        (AgentExitReason.FAILED, AgentSessionStatus.FAILED),
        (AgentExitReason.TIMED_OUT, AgentSessionStatus.TIMED_OUT),
    ],
)
async def test_fake_adapter_simulates_unsuccessful_results(
    reason: AgentExitReason,
    expected_status: AgentSessionStatus,
) -> None:
    adapter = FakeAgentAdapter(
        FakeAgentScenario(reason=reason, exit_code=1, error="simulated failure")
    )

    session = await adapter.start(make_request())
    result = await adapter.wait(session.session_id)

    assert result.reason is reason
    assert result.error == "simulated failure"
    assert session.status is expected_status


@pytest.mark.asyncio
async def test_fake_adapter_can_fail_during_start() -> None:
    adapter = FakeAgentAdapter(FakeAgentScenario(start_error="provider unavailable"))

    with pytest.raises(AgentAdapterError, match="provider unavailable"):
        await adapter.start(make_request())


@pytest.mark.asyncio
async def test_blocking_session_can_be_cancelled_repeatedly() -> None:
    adapter = FakeAgentAdapter(FakeAgentScenario(block_until_cancel=True))
    session = await adapter.start(make_request())

    await adapter.cancel(session.session_id)
    await adapter.cancel(session.session_id)
    result = await adapter.wait(session.session_id)
    events = [event async for event in adapter.stream(session.session_id)]

    assert result.reason is AgentExitReason.CANCELLED
    assert session.status is AgentSessionStatus.CANCELLED
    assert events[-1].type is AgentEventType.CANCELLED


@pytest.mark.asyncio
async def test_resume_maps_native_id_to_a_new_local_session() -> None:
    adapter = FakeAgentAdapter()

    session = await adapter.resume("native-123", make_request())

    assert session.native_session_id == "native-123"
    assert await adapter.wait(session.session_id)


@pytest.mark.asyncio
async def test_resume_requires_capability() -> None:
    adapter = FakeAgentAdapter(capabilities=frozenset())

    with pytest.raises(AgentAdapterError, match="does not support"):
        await adapter.resume("native-123", make_request())

    assert adapter.capabilities == frozenset()


@pytest.mark.asyncio
async def test_unknown_session_is_rejected() -> None:
    adapter = FakeAgentAdapter()

    with pytest.raises(AgentSessionNotFoundError, match="unknown session"):
        await adapter.wait(uuid4())


def test_fake_event_delay_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        FakeEventSpec(delay_seconds=-0.1)
