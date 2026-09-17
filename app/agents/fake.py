import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from uuid import UUID

from app.agents.base import AgentAdapter, AgentAdapterError, AgentSessionNotFoundError
from app.agents.models import (
    AgentCapability,
    AgentEvent,
    AgentEventType,
    AgentExitReason,
    AgentRequest,
    AgentResult,
    AgentSession,
    AgentSessionStatus,
    TokenUsage,
)


@dataclass(frozen=True, slots=True)
class FakeEventSpec:
    type: AgentEventType = AgentEventType.MESSAGE
    text: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    delay_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")


@dataclass(frozen=True, slots=True)
class FakeAgentScenario:
    """Deterministic behavior for sessions created by a fake adapter."""

    events: tuple[FakeEventSpec, ...] = ()
    reason: AgentExitReason = AgentExitReason.COMPLETED
    exit_code: int | None = 0
    output: Mapping[str, Any] = field(default_factory=dict)
    token_usage: TokenUsage | None = None
    error: str | None = None
    start_error: str | None = None
    block_until_cancel: bool = False


_END_OF_EVENTS = object()


@dataclass(slots=True)
class _FakeSessionState:
    session: AgentSession
    request: AgentRequest
    queue: asyncio.Queue[AgentEvent | object]
    cancel_requested: asyncio.Event
    completion: asyncio.Task[AgentResult] | None = None
    stream_claimed: bool = False


class FakeAgentAdapter(AgentAdapter):
    """In-memory adapter with deterministic, configurable lifecycle behavior."""

    def __init__(
        self,
        scenario: FakeAgentScenario | None = None,
        *,
        name: str = "fake",
        capabilities: frozenset[AgentCapability] | None = None,
    ) -> None:
        if not name:
            raise ValueError("name must not be empty")
        self._name = name
        self._scenario = scenario or FakeAgentScenario()
        self._capabilities = (
            frozenset(AgentCapability) if capabilities is None else capabilities
        )
        self._sessions: dict[UUID, _FakeSessionState] = {}
        self.requests: list[AgentRequest] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> frozenset[AgentCapability]:
        return self._capabilities

    async def start(self, request: AgentRequest) -> AgentSession:
        if self._scenario.start_error is not None:
            raise AgentAdapterError(self._scenario.start_error)

        self.requests.append(request.model_copy(deep=True))
        session = AgentSession(
            task_id=request.task_id,
            trace_id=request.trace_id,
            agent_name=self.name,
            role=request.role,
            status=AgentSessionStatus.RUNNING,
        )
        state = _FakeSessionState(
            session=session,
            request=request,
            queue=asyncio.Queue(),
            cancel_requested=asyncio.Event(),
        )
        self._sessions[session.session_id] = state
        state.completion = asyncio.create_task(self._run(state))
        return session

    async def stream(self, session_id: UUID) -> AsyncIterator[AgentEvent]:
        state = self._get_state(session_id)
        if state.stream_claimed:
            raise AgentAdapterError("session event stream can only be consumed once")
        state.stream_claimed = True

        while True:
            item = await state.queue.get()
            if item is _END_OF_EVENTS:
                return
            if isinstance(item, AgentEvent):
                yield item

    async def wait(self, session_id: UUID) -> AgentResult:
        state = self._get_state(session_id)
        if state.completion is None:
            raise AgentAdapterError("fake session was not scheduled")
        return await asyncio.shield(state.completion)

    async def cancel(self, session_id: UUID) -> None:
        state = self._get_state(session_id)
        if state.completion is None or state.completion.done():
            return
        state.cancel_requested.set()
        await self.wait(session_id)

    async def resume(self, native_session_id: str, request: AgentRequest) -> AgentSession:
        if not native_session_id:
            raise ValueError("native_session_id must not be empty")
        if not self.supports(AgentCapability.SESSION_RESUME):
            raise AgentAdapterError(f"adapter {self.name!r} does not support session resume")
        session = await self.start(request)
        session.native_session_id = native_session_id
        return session

    def _get_state(self, session_id: UUID) -> _FakeSessionState:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise AgentSessionNotFoundError(f"unknown session: {session_id}") from exc

    async def _run(self, state: _FakeSessionState) -> AgentResult:
        started_at = monotonic()
        sequence = 0
        self._emit(state, sequence, AgentEventType.STARTED)
        sequence += 1

        for spec in self._scenario.events:
            if await self._cancelled_during(state, spec.delay_seconds):
                return self._finish_cancelled(state, sequence, started_at)
            self._emit(state, sequence, spec.type, text=spec.text, data=dict(spec.data))
            sequence += 1

        if self._scenario.block_until_cancel:
            await state.cancel_requested.wait()
            return self._finish_cancelled(state, sequence, started_at)

        if state.cancel_requested.is_set():
            return self._finish_cancelled(state, sequence, started_at)

        terminal_type, session_status = self._terminal_state(self._scenario.reason)
        self._emit(state, sequence, terminal_type, text=self._scenario.error)
        state.session.status = session_status
        result = AgentResult(
            session_id=state.session.session_id,
            trace_id=state.session.trace_id,
            reason=self._scenario.reason,
            exit_code=self._scenario.exit_code,
            output=dict(self._scenario.output),
            token_usage=self._scenario.token_usage,
            duration_ms=self._duration_ms(started_at),
            error=self._scenario.error,
        )
        state.queue.put_nowait(_END_OF_EVENTS)
        return result

    async def _cancelled_during(self, state: _FakeSessionState, delay_seconds: float) -> bool:
        if state.cancel_requested.is_set():
            return True
        if delay_seconds == 0:
            return False
        try:
            await asyncio.wait_for(state.cancel_requested.wait(), timeout=delay_seconds)
        except TimeoutError:
            return False
        return True

    def _finish_cancelled(
        self,
        state: _FakeSessionState,
        sequence: int,
        started_at: float,
    ) -> AgentResult:
        self._emit(state, sequence, AgentEventType.CANCELLED)
        state.session.status = AgentSessionStatus.CANCELLED
        result = AgentResult(
            session_id=state.session.session_id,
            trace_id=state.session.trace_id,
            reason=AgentExitReason.CANCELLED,
            exit_code=None,
            duration_ms=self._duration_ms(started_at),
        )
        state.queue.put_nowait(_END_OF_EVENTS)
        return result

    @staticmethod
    def _emit(
        state: _FakeSessionState,
        sequence: int,
        event_type: AgentEventType,
        *,
        text: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        state.queue.put_nowait(
            AgentEvent(
                session_id=state.session.session_id,
                trace_id=state.session.trace_id,
                sequence=sequence,
                type=event_type,
                text=text,
                data=data or {},
            )
        )

    @staticmethod
    def _terminal_state(
        reason: AgentExitReason,
    ) -> tuple[AgentEventType, AgentSessionStatus]:
        if reason is AgentExitReason.COMPLETED:
            return AgentEventType.COMPLETED, AgentSessionStatus.COMPLETED
        if reason is AgentExitReason.CANCELLED:
            return AgentEventType.CANCELLED, AgentSessionStatus.CANCELLED
        if reason is AgentExitReason.TIMED_OUT:
            return AgentEventType.FAILED, AgentSessionStatus.TIMED_OUT
        return AgentEventType.FAILED, AgentSessionStatus.FAILED

    @staticmethod
    def _duration_ms(started_at: float) -> int:
        return max(0, int((monotonic() - started_at) * 1000))
