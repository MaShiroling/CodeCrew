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
class _ClaudeSessionState:
    session: AgentSession
    process: ManagedProcess
    queue: asyncio.Queue[AgentEvent | object] = field(default_factory=asyncio.Queue)
    completion: asyncio.Task[AgentResult] | None = None
    stream_claimed: bool = False
    sequence: int = 0
    result_payload: dict[str, Any] | None = None


class ClaudeCodeAdapter(AgentAdapter):
    """Read-only Claude Code adapter using its streaming JSON print mode."""

    def __init__(
        self,
        *,
        executable: str = "claude",
        runner: _ProcessRunner | None = None,
    ) -> None:
        if not executable:
            raise ValueError("executable must not be empty")
        self._executable = executable
        self._runner = runner or AsyncProcessRunner()
        self._sessions: dict[UUID, _ClaudeSessionState] = {}

    @property
    def name(self) -> str:
        return "claude-code"

    @property
    def capabilities(self) -> frozenset[AgentCapability]:
        return frozenset(
            {
                AgentCapability.REPOSITORY_ANALYSIS,
                AgentCapability.CODE_REVIEW,
                AgentCapability.STREAMING,
                AgentCapability.SESSION_RESUME,
            }
        )

    async def start(self, request: AgentRequest) -> AgentSession:
        if request.permission_mode is not PermissionMode.READ_ONLY:
            raise AgentAdapterError("Claude Code adapter currently accepts read-only requests only")

        argv = self.build_command(request)
        try:
            process = await self._runner.start(
                argv,
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
        state = _ClaudeSessionState(session=session, process=process)
        self._sessions[session.session_id] = state
        state.completion = asyncio.create_task(self._consume(state))
        return session

    def stream(self, session_id: UUID) -> AsyncIterator[AgentEvent]:
        return self._stream(session_id)

    async def wait(self, session_id: UUID) -> AgentResult:
        state = self._get_state(session_id)
        if state.completion is None:
            raise AgentAdapterError("Claude Code session was not scheduled")
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
        command = [
            self._executable,
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            "--safe-mode",
            "--disable-slash-commands",
            "--strict-mcp-config",
            '--mcp-config={"mcpServers":{}}',
            "--permission-mode",
            "plan",
            "--tools=Read,Glob,Grep",
        ]
        if request.resume_from_session_id is not None:
            command.extend(["--resume", request.resume_from_session_id])
        command.append(request.prompt)
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

    async def _consume(self, state: _ClaudeSessionState) -> AgentResult:
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

    def _consume_stdout(self, state: _ClaudeSessionState, chunk: ProcessChunk) -> None:
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
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            state.session.native_session_id = session_id

        if native_type == "assistant":
            self._consume_assistant_message(state, payload)
        elif native_type == "result":
            state.result_payload = payload
        elif not (
            native_type == "system"
            and payload.get("subtype") in {"init", "thinking_tokens"}
        ):
            self._emit(
                state,
                AgentEventType.MESSAGE,
                data=payload,
                native_event_type=str(native_type) if native_type is not None else None,
            )

    def _consume_assistant_message(
        self,
        state: _ClaudeSessionState,
        payload: dict[str, Any],
    ) -> None:
        message = payload.get("message")
        content = message.get("content", []) if isinstance(message, dict) else []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                self._emit(
                    state,
                    AgentEventType.MESSAGE,
                    text=block["text"],
                    native_event_type="assistant.text",
                )
            elif block_type == "tool_use":
                self._emit(
                    state,
                    AgentEventType.TOOL_CALL,
                    data=block,
                    native_event_type="assistant.tool_use",
                )

    def _build_result(
        self,
        state: _ClaudeSessionState,
        process_result: ProcessResult,
    ) -> AgentResult:
        payload = state.result_payload or {}
        is_error = bool(payload.get("is_error"))
        if process_result.cancelled:
            reason = AgentExitReason.CANCELLED
            status = AgentSessionStatus.CANCELLED
            event_type = AgentEventType.CANCELLED
        elif process_result.timed_out:
            reason = AgentExitReason.TIMED_OUT
            status = AgentSessionStatus.TIMED_OUT
            event_type = AgentEventType.FAILED
        elif process_result.exit_code != 0 or is_error:
            reason = AgentExitReason.FAILED
            status = AgentSessionStatus.FAILED
            event_type = AgentEventType.FAILED
        else:
            reason = AgentExitReason.COMPLETED
            status = AgentSessionStatus.COMPLETED
            event_type = AgentEventType.COMPLETED

        state.session.status = status
        error = self._result_error(payload) if reason is AgentExitReason.FAILED else None
        self._emit(state, event_type, text=error)
        return AgentResult(
            session_id=state.session.session_id,
            trace_id=state.session.trace_id,
            reason=reason,
            exit_code=process_result.exit_code,
            output=self._result_output(payload),
            token_usage=self._token_usage(payload),
            duration_ms=self._duration_ms(payload, process_result),
            error=error,
        )

    def _emit(
        self,
        state: _ClaudeSessionState,
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

    def _get_state(self, session_id: UUID) -> _ClaudeSessionState:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise AgentSessionNotFoundError(f"unknown session: {session_id}") from exc

    @staticmethod
    def _result_output(payload: dict[str, Any]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        if "result" in payload:
            output["result"] = payload["result"]
        if "total_cost_usd" in payload:
            output["total_cost_usd"] = payload["total_cost_usd"]
        return output

    @staticmethod
    def _result_error(payload: dict[str, Any]) -> str | None:
        result = payload.get("result")
        return result if isinstance(result, str) and result else None

    @staticmethod
    def _token_usage(payload: dict[str, Any]) -> TokenUsage | None:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None
        return TokenUsage(
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cached_input_tokens=usage.get("cache_read_input_tokens"),
        )

    @staticmethod
    def _duration_ms(payload: dict[str, Any], process_result: ProcessResult) -> int:
        duration = payload.get("duration_ms")
        return duration if isinstance(duration, int) and duration >= 0 else process_result.duration_ms
