from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.agents import (
    AgentAdapter,
    AgentCapability,
    AgentEvent,
    AgentEventType,
    AgentExitReason,
    AgentRequest,
    AgentResult,
    AgentRole,
    AgentSession,
    AgentSessionStatus,
)


class ContractAdapter(AgentAdapter):
    """Minimal test double proving that the abstract contract is implementable."""

    def __init__(self) -> None:
        self.sessions: dict[UUID, AgentSession] = {}
        self.cancelled: set[UUID] = set()

    @property
    def name(self) -> str:
        return "contract"

    @property
    def capabilities(self) -> frozenset[AgentCapability]:
        return frozenset({AgentCapability.STREAMING, AgentCapability.SESSION_RESUME})

    async def start(self, request: AgentRequest) -> AgentSession:
        session = AgentSession(
            task_id=request.task_id,
            trace_id=request.trace_id,
            agent_name=self.name,
            role=request.role,
            status=AgentSessionStatus.RUNNING,
        )
        self.sessions[session.session_id] = session
        return session

    async def stream(self, session_id: UUID) -> AsyncIterator[AgentEvent]:
        session = self.sessions[session_id]
        yield AgentEvent(
            session_id=session_id,
            trace_id=session.trace_id,
            sequence=0,
            type=AgentEventType.STARTED,
        )

    async def wait(self, session_id: UUID) -> AgentResult:
        session = self.sessions[session_id]
        return AgentResult(
            session_id=session_id,
            trace_id=session.trace_id,
            reason=AgentExitReason.COMPLETED,
            exit_code=0,
            duration_ms=0,
        )

    async def cancel(self, session_id: UUID) -> None:
        self.cancelled.add(session_id)

    async def resume(self, native_session_id: str, request: AgentRequest) -> AgentSession:
        session = await self.start(request)
        session.native_session_id = native_session_id
        return session


class IncompleteAdapter(AgentAdapter):
    @property
    def name(self) -> str:
        return "incomplete"

    @property
    def capabilities(self) -> frozenset[AgentCapability]:
        return frozenset()


def make_request() -> AgentRequest:
    return AgentRequest(
        task_id=uuid4(),
        trace_id=uuid4(),
        role=AgentRole.PLANNER,
        prompt="Return a plan.",
        working_directory=Path("/tmp/repository"),
    )


def test_incomplete_adapter_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        IncompleteAdapter()


@pytest.mark.asyncio
async def test_adapter_contract_covers_full_session_lifecycle() -> None:
    adapter = ContractAdapter()
    request = make_request()

    session = await adapter.start(request)
    events = [event async for event in adapter.stream(session.session_id)]
    result = await adapter.wait(session.session_id)
    await adapter.cancel(session.session_id)

    assert events[0].type is AgentEventType.STARTED
    assert result.reason is AgentExitReason.COMPLETED
    assert session.session_id in adapter.cancelled


@pytest.mark.asyncio
async def test_resume_maps_native_session_to_new_local_session() -> None:
    adapter = ContractAdapter()

    session = await adapter.resume("provider-session-1", make_request())

    assert session.native_session_id == "provider-session-1"
    assert session.session_id in adapter.sessions


def test_supports_uses_declared_capabilities() -> None:
    adapter = ContractAdapter()

    assert adapter.supports(AgentCapability.STREAMING)
    assert not adapter.supports(AgentCapability.CODE_EDIT)

