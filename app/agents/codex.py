import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
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
    PermissionMode,
    TokenUsage,
)
from app.agents.process import (
    AsyncProcessRunner,
    ManagedProcess,
    ProcessChunk,
    ProcessResult,
    ProcessStartError,
    ProcessStream,
)


class _ProcessRunner(Protocol):
    async def start(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
    ) -> ManagedProcess: ...


_END_OF_EVENTS = object()


@dataclass(slots=True)
class _CodexSessionState:
    session: AgentSession
    process: ManagedProcess
    queue: asyncio.Queue[AgentEvent | object] = field(default_factory=asyncio.Queue)
    completion: asyncio.Task[AgentResult] | None = None
    stream_claimed: bool = False
    sequence: int = 0
    turn_payload: dict[str, Any] | None = None
    error: str | None = None
    last_message: str | None = None


class CodexCliAdapter(AgentAdapter):
    """Codex CLI adapter using non-interactive JSONL execution."""

    def __init__(
        self,
        *,
        executable: str = "codex",
        runner: _ProcessRunner | None = None,
    ) -> None:
        if not executable:
            raise ValueError("executable must not be empty")
        self._executable = executable
        self._runner = runner or AsyncProcessRunner()
        self._sessions: dict[UUID, _CodexSessionState] = {}

    @property
    def name(self) -> str:
        return "codex-cli"

    @property
    def capabilities(self) -> frozenset[AgentCapability]:
        return frozenset(
            {
                AgentCapability.REPOSITORY_ANALYSIS,
                AgentCapability.CODE_EDIT,
                AgentCapability.COMMAND_EXECUTION,
                AgentCapability.STREAMING,
                AgentCapability.SESSION_RESUME,
            }
        )

    async def start(self, request: AgentRequest) -> AgentSession:
        try:
            process = await self._runner.start(
                self.build_command(request),
                cwd=request.working_directory,
                timeout_seconds=request.timeout_seconds,
            )
        except ProcessStartError as exc:
            raise AgentAdapterError(str(exc)) from exc

        session = AgentSession(
            task_id=request.task_id,
            trace_id=request.trace_id,
            agent_name=self.name,
            role=request.role,
            status=AgentSessionStatus.RUNNING,
        )
        state = _CodexSessionState(session=session, process=process)
        self._sessions[session.session_id] = state
        state.completion = asyncio.create_task(self._consume(state))
        return session

    def stream(self, session_id: UUID) -> AsyncIterator[AgentEvent]:
        return self._stream(session_id)

    async def wait(self, session_id: UUID) -> AgentResult:
        state = self._get_state(session_id)
        if state.completion is None:
            raise AgentAdapterError("Codex CLI session was not scheduled")
        return await asyncio.shield(state.completion)

    async def cancel(self, session_id: UUID) -> None:
        state = self._get_state(session_id)
        if state.completion is None or state.completion.done():
            return
        await state.process.cancel()
        await self.wait(session_id)

    async def resume(self, native_session_id: str, request: AgentRequest) -> AgentSession:
        if not native_session_id:
            raise ValueError("native_session_id must not be empty")
        resumed_request = request.model_copy(
            update={"resume_from_session_id": native_session_id}
        )
        return await self.start(resumed_request)

    def build_command(self, request: AgentRequest) -> list[str]:
        sandbox = (
            "read-only"
            if request.permission_mode is PermissionMode.READ_ONLY
            else "workspace-write"
        )
        command = [
            self._executable,
            "--sandbox",
            sandbox,
            "--ask-for-approval",
            "never",
            "--cd",
            str(request.working_directory),
            "exec",
        ]
        if request.resume_from_session_id is not None:
            command.extend(["resume", request.resume_from_session_id])
        command.extend(["--json", "--ignore-user-config", "--ignore-rules", request.prompt])
        return command

    async def _stream(self, session_id: UUID) -> AsyncIterator[AgentEvent]:
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

    async def _consume(self, state: _CodexSessionState) -> AgentResult:
        self._emit(state, AgentEventType.STARTED)
        try:
            async for chunk in state.process.stream():
                if chunk.stream is ProcessStream.STDERR:
                    self._emit(state, AgentEventType.STDERR, text=chunk.text)
                else:
                    self._consume_stdout(state, chunk)
            process_result = await state.process.wait()
            result = self._build_result(state, process_result)
        except Exception as exc:  # noqa: BLE001 - adapter boundary must close its event stream.
            state.session.status = AgentSessionStatus.FAILED
            self._emit(state, AgentEventType.FAILED, text=str(exc))
            result = AgentResult(
                session_id=state.session.session_id,
                trace_id=state.session.trace_id,
                reason=AgentExitReason.FAILED,
                exit_code=None,
                duration_ms=0,
                error=str(exc),
            )
        state.queue.put_nowait(_END_OF_EVENTS)
        return result

    def _consume_stdout(self, state: _CodexSessionState, chunk: ProcessChunk) -> None:
        line = chunk.text.strip()
        if not line:
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            self._emit(state, AgentEventType.STDOUT, text=chunk.text)
            return
        if not isinstance(payload, dict):
            self._emit(state, AgentEventType.STDOUT, text=chunk.text)
            return

        native_type = payload.get("type")
        if native_type == "thread.started":
            thread_id = payload.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                state.session.native_session_id = thread_id
        elif native_type in {"item.started", "item.completed"}:
            self._consume_item(state, payload)
        elif native_type == "turn.completed":
            state.turn_payload = payload
        elif native_type in {"turn.failed", "error"}:
            state.error = self._extract_error(payload)
            self._emit(
                state,
                AgentEventType.MESSAGE,
                text=state.error,
                data=payload,
                native_event_type=str(native_type),
            )
        elif native_type != "turn.started":
            self._emit(
                state,
                AgentEventType.MESSAGE,
                data=payload,
                native_event_type=str(native_type) if native_type is not None else None,
            )

    def _consume_item(self, state: _CodexSessionState, payload: dict[str, Any]) -> None:
        item = payload.get("item")
        if not isinstance(item, dict):
            return
        item_type = item.get("type")
        native_type = f"{payload.get('type')}.{item_type}"
        if item_type == "agent_message" and payload.get("type") == "item.completed":
            text = item.get("text")
            if isinstance(text, str):
                state.last_message = text
                self._emit(
                    state,
                    AgentEventType.MESSAGE,
                    text=text,
                    native_event_type=native_type,
                )
        elif item_type in {"command_execution", "file_change", "mcp_tool_call"}:
            self._emit(
                state,
                AgentEventType.TOOL_CALL,
                data=item,
                native_event_type=native_type,
            )

    def _build_result(
        self,
        state: _CodexSessionState,
        process_result: ProcessResult,
    ) -> AgentResult:
        if process_result.cancelled:
            reason = AgentExitReason.CANCELLED
            status = AgentSessionStatus.CANCELLED
            event_type = AgentEventType.CANCELLED
        elif process_result.timed_out:
            reason = AgentExitReason.TIMED_OUT
            status = AgentSessionStatus.TIMED_OUT
            event_type = AgentEventType.FAILED
        elif process_result.exit_code != 0 or state.error is not None:
            reason = AgentExitReason.FAILED
            status = AgentSessionStatus.FAILED
            event_type = AgentEventType.FAILED
        else:
            reason = AgentExitReason.COMPLETED
            status = AgentSessionStatus.COMPLETED
            event_type = AgentEventType.COMPLETED

        state.session.status = status
        self._emit(state, event_type, text=state.error)
        output = {"message": state.last_message} if state.last_message is not None else {}
        return AgentResult(
            session_id=state.session.session_id,
            trace_id=state.session.trace_id,
            reason=reason,
            exit_code=process_result.exit_code,
            output=output,
            token_usage=self._token_usage(state.turn_payload),
            duration_ms=process_result.duration_ms,
            error=state.error if reason is AgentExitReason.FAILED else None,
        )

    def _emit(
        self,
        state: _CodexSessionState,
        event_type: AgentEventType,
        *,
        text: str | None = None,
        data: dict[str, Any] | None = None,
        native_event_type: str | None = None,
    ) -> None:
        state.queue.put_nowait(
            AgentEvent(
                session_id=state.session.session_id,
                trace_id=state.session.trace_id,
                sequence=state.sequence,
                type=event_type,
                text=text,
                data=data or {},
                native_event_type=native_event_type,
            )
        )
        state.sequence += 1

    def _get_state(self, session_id: UUID) -> _CodexSessionState:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise AgentSessionNotFoundError(f"unknown session: {session_id}") from exc

    @staticmethod
    def _extract_error(payload: dict[str, Any]) -> str:
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        message = payload.get("message")
        return message if isinstance(message, str) else "Codex CLI reported an error"

    @staticmethod
    def _token_usage(payload: dict[str, Any] | None) -> TokenUsage | None:
        if payload is None:
            return None
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None
        return TokenUsage(
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cached_input_tokens=usage.get("cached_input_tokens"),
        )

