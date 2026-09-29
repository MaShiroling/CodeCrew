import asyncio
import json
import os
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
    turn_failed: bool = False
    last_message: str | None = None


class CodexCliAdapter(AgentAdapter):
    """Codex CLI adapter using non-interactive JSONL execution."""

    _CHAT_PASSTHROUGH_ENV = (
        "PATH", "HOME", "CODEX_HOME", "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "OPENAI_ORGANIZATION", "OPENAI_PROJECT", "LANG", "LC_ALL", "USER",
        "TERM", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "NODE_EXTRA_CA_CERTS",
        "SSL_CERT_FILE",
    )

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
        if request.standalone_chat_room_id is not None:
            try:
                request = AgentRequest.model_validate(request.model_dump())
            except ValueError as exc:
                raise AgentAdapterError("invalid standalone chat request") from exc
        try:
            process = await self._runner.start(
                self.build_command(request),
                cwd=request.working_directory,
                timeout_seconds=request.timeout_seconds,
                env=self.build_process_env(request),
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
        resumed_request = request.model_copy(update={"resume_from_session_id": native_session_id})
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
        if request.standalone_chat_room_id is not None:
            command.append("--skip-git-repo-check")
        command.extend(["--json", "--ignore-user-config", "--ignore-rules", request.prompt])
        return command

    def build_process_env(self, request: AgentRequest) -> Mapping[str, str] | None:
        if request.standalone_chat_room_id is None:
            return None
        # Keep Codex's existing auth store, without passing unrelated secrets.
        environment = {
            name: value for name in self._CHAT_PASSTHROUGH_ENV
            if (value := os.environ.get(name)) is not None
        }
        environment["TMPDIR"] = str(request.runtime_directory / "tmp")
        return environment

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
            # Transport errors can precede a successful HTTPS fallback. An explicit
            # failed turn is terminal, however, and must never be cleared.
            if not state.turn_failed:
                state.error = None
        elif native_type in {"turn.failed", "error"}:
            diagnostic = self._extract_error(payload)
            if native_type == "turn.failed":
                state.turn_failed = True
                state.error = diagnostic
            elif not state.turn_failed:
                state.error = diagnostic
            self._emit(
                state,
                AgentEventType.MESSAGE,
                text=diagnostic,
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
        elif item_type == "error":
            diagnostic = self._extract_error(item)
            if not state.turn_failed:
                state.error = diagnostic
            self._emit(
                state,
                AgentEventType.MESSAGE,
                text=diagnostic,
                data=payload,
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
        elif (
            process_result.exit_code != 0
            or state.turn_failed
            or state.error is not None
            or state.turn_payload is None
        ):
            reason = AgentExitReason.FAILED
            status = AgentSessionStatus.FAILED
            event_type = AgentEventType.FAILED
            if state.error is None:
                state.error = (
                    f"Codex CLI exited with code {process_result.exit_code}"
                    if process_result.exit_code != 0
                    else "Codex stream ended without turn.completed"
                )
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
