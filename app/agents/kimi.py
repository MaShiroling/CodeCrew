"""Opt-in Kimi Code CLI implementer with a restricted tool and write boundary."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from app.agents.artifact_inputs import verify_artifact_files
from app.agents.base import AgentAdapter, AgentAdapterError, AgentSessionNotFoundError
from app.agents.kimi_boundary import KimiBoundaryError, KimiWriteBoundary
from app.agents.models import (
    AgentArtifactInput,
    AgentCapability,
    AgentEvent,
    AgentEventType,
    AgentExitReason,
    AgentRequest,
    AgentResult,
    AgentRole,
    AgentSession,
    AgentSessionStatus,
    PermissionMode,
)
from app.agents.process import (
    AsyncProcessRunner,
    ManagedProcess,
    ProcessChunk,
    ProcessResult,
    ProcessStartError,
    ProcessStream,
)

if TYPE_CHECKING:
    from app.workspace.permissions import PermissionPolicy


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
_ALLOWED_TOOLS = frozenset({"Read", "Grep", "Glob", "Write", "Edit"})
_INFORMATIONAL_META_TYPES = frozenset(
    {"system.version", "turn.step.retrying", "session.resume_hint"}
)
_AGENT_FRONTMATTER = """name: codecrew-restricted-implementer
description: Implement a CodeCrew task inside its assigned worktree without shell access
tools:
  - Read
  - Grep
  - Glob
  - Write
  - Edit
subagents: []"""


@dataclass(slots=True)
class _KimiSessionState:
    session: AgentSession
    process: ManagedProcess
    artifact_inputs: tuple[AgentArtifactInput, ...] = ()
    queue: asyncio.Queue[AgentEvent | object] = field(default_factory=asyncio.Queue)
    completion: asyncio.Task[AgentResult] | None = None
    stream_claimed: bool = False
    sequence: int = 0
    last_message: str | None = None
    error: str | None = None


class KimiCodeAdapter(AgentAdapter):
    """Kimi CLI for file edits only; deterministic tests run outside this adapter.

    A fresh session is required for every turn because Kimi cannot combine
    ``--agent-file`` with ``--session``. Native session IDs and token usage are
    not present in the documented stream-json protocol, so neither is inferred.
    """

    # Kimi Code membership API is separate from the Moonshot Platform API.
    # The fixed model alias may point to a changing backend model over time.
    MODEL = "kimi-for-coding"
    BASE_URL = "https://api.kimi.com/coding/v1"
    _PASSTHROUGH_ENV = (
        "PATH",
        "LANG",
        "LC_ALL",
        "USER",
        "TERM",
        "NODE_EXTRA_CA_CERTS",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
    )

    def __init__(
        self,
        *,
        worktree_root: Path,
        runtime_root: Path,
        policy: PermissionPolicy,
        executable: str = "kimi",
        runner: _ProcessRunner | None = None,
        env_source: Mapping[str, str] | None = None,
        boundary_factory: Callable[..., KimiWriteBoundary] = KimiWriteBoundary,
        max_steps_per_turn: int | None = None,
    ) -> None:
        if not executable:
            raise ValueError("executable must not be empty")
        if max_steps_per_turn is not None and max_steps_per_turn <= 0:
            raise ValueError("max_steps_per_turn must be positive")
        self._worktree_root = worktree_root.resolve()
        self._runtime_root = runtime_root.absolute()
        self._policy = policy
        self._executable = executable
        self._runner = runner or AsyncProcessRunner()
        self._env_source = os.environ if env_source is None else env_source
        self._boundary_factory = boundary_factory
        self._max_steps_per_turn = max_steps_per_turn
        self._agent_file = (
            Path(__file__).resolve().parent / "assets" / "kimi_restricted_implementer.md"
        )
        self._sessions: dict[UUID, _KimiSessionState] = {}

    @property
    def name(self) -> str:
        return "kimi-code-cli"

    @property
    def capabilities(self) -> frozenset[AgentCapability]:
        return frozenset(
            {
                AgentCapability.REPOSITORY_ANALYSIS,
                AgentCapability.CODE_EDIT,
                AgentCapability.STREAMING,
            }
        )

    async def start(self, request: AgentRequest) -> AgentSession:
        if request.role is not AgentRole.IMPLEMENTER:
            raise AgentAdapterError("Kimi Code adapter accepts implementer requests only")
        if request.permission_mode is not PermissionMode.WORKSPACE_WRITE:
            raise AgentAdapterError("Kimi Code adapter requires workspace-write permission")
        if request.resume_from_session_id is not None:
            raise AgentAdapterError("Kimi resume is disabled with the restricted agent file")
        key = self._env_source.get("KIMI_MODEL_API_KEY", "").strip()
        if not key:
            raise AgentAdapterError(
                "KIMI_MODEL_API_KEY (Kimi Code membership key) is required for isolated Kimi CLI"
            )
        worktree = self._validate_worktree(request)
        input_paths = await asyncio.to_thread(verify_artifact_files, request.artifact_inputs)
        if any(
            path.is_relative_to(worktree) or path.is_relative_to(self._runtime_root.resolve())
            for path in input_paths
        ):
            raise AgentAdapterError("Artifact inputs must be outside writable worktree and runtime")
        session = AgentSession(
            task_id=request.task_id,
            trace_id=request.trace_id,
            agent_name=self.name,
            role=request.role,
            status=AgentSessionStatus.RUNNING,
        )
        runtime = self._prepare_runtime(request.task_id, session.session_id)
        self._validate_agent_file()
        try:
            boundary = self._boundary_factory(
                worktree=worktree,
                runtime_directory=runtime,
                policy=self._policy,
                readable_files=(
                    self._agent_file,
                    Path(shutil.which(self._executable) or self._executable),
                ),
                read_only_files=input_paths,
            )
            command = boundary.wrap(self.build_command(request))
        except KimiBoundaryError as exc:
            raise AgentAdapterError(str(exc)) from exc
        try:
            process = await self._runner.start(
                command,
                cwd=worktree,
                timeout_seconds=request.timeout_seconds,
                env=self.build_process_env(runtime, key),
            )
        except ProcessStartError as exc:
            raise AgentAdapterError(str(exc)) from exc

        state = _KimiSessionState(
            session=session, process=process, artifact_inputs=request.artifact_inputs
        )
        self._sessions[session.session_id] = state
        state.completion = asyncio.create_task(self._consume(state))
        return session

    def build_command(self, request: AgentRequest) -> list[str]:
        return [
            self._executable,
            "--prompt",
            request.prompt,
            "--output-format",
            "stream-json",
            "--agent-file",
            str(self._agent_file),
        ]

    def build_process_env(self, runtime: Path, key: str) -> dict[str, str]:
        environment = {
            name: value
            for name in self._PASSTHROUGH_ENV
            if (value := self._env_source.get(name)) is not None
        }
        environment.update(
            {
                "HOME": str(runtime / "home"),
                "KIMI_CODE_HOME": str(runtime / "kimi-home"),
                "TMPDIR": str(runtime / "tmp"),
                "KIMI_MODEL_NAME": self.MODEL,
                "KIMI_MODEL_PROVIDER_TYPE": "kimi",
                "KIMI_MODEL_BASE_URL": self.BASE_URL,
                "KIMI_MODEL_API_KEY": key,
                "KIMI_DISABLE_CRON": "1",
                "KIMI_DISABLE_TELEMETRY": "1",
                "KIMI_CODE_NO_AUTO_UPDATE": "1",
            }
        )
        if self._max_steps_per_turn is not None:
            environment["KIMI_LOOP_MAX_STEPS_PER_TURN"] = str(self._max_steps_per_turn)
        return environment

    def _validate_worktree(self, request: AgentRequest) -> Path:
        expected = self._worktree_root / str(request.task_id)
        if expected.is_symlink() or not expected.is_dir() or not (expected / ".git").exists():
            raise AgentAdapterError("request does not name a managed Git worktree")
        worktree = expected.resolve(strict=True)
        if (
            worktree.parent != self._worktree_root
            or request.working_directory.resolve() != worktree
        ):
            raise AgentAdapterError("request escaped its task-owned worktree")
        return worktree

    def _prepare_runtime(self, task_id: UUID, session_id: UUID) -> Path:
        if self._runtime_root.is_symlink():
            raise AgentAdapterError("Kimi runtime root must not be a symlink")
        task_runtime = self._runtime_root / str(task_id)
        runtime = task_runtime / str(session_id)
        for path in (
            self._runtime_root,
            task_runtime,
            runtime,
            runtime / "home",
            runtime / "kimi-home",
            runtime / "tmp",
        ):
            if path.is_symlink():
                raise AgentAdapterError(f"Kimi runtime directory must not be a symlink: {path}")
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise AgentAdapterError(f"Kimi runtime directory is accessible by others: {path}")
        return runtime

    def _validate_agent_file(self) -> None:
        try:
            contents = self._agent_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise AgentAdapterError("restricted Kimi agent file is unavailable") from exc
        parts = contents.split("---", maxsplit=2)
        if len(parts) != 3 or parts[0].strip() or parts[1].strip() != _AGENT_FRONTMATTER:
            raise AgentAdapterError("restricted Kimi agent file changed unexpectedly")

    def stream(self, session_id: UUID) -> AsyncIterator[AgentEvent]:
        return self._stream(session_id)

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

    async def wait(self, session_id: UUID) -> AgentResult:
        state = self._get_state(session_id)
        if state.completion is None:
            raise AgentAdapterError("Kimi Code session was not scheduled")
        return await asyncio.shield(state.completion)

    async def cancel(self, session_id: UUID) -> None:
        state = self._get_state(session_id)
        if state.completion is None or state.completion.done():
            return
        await state.process.cancel()
        await self.wait(session_id)

    async def resume(self, native_session_id: str, request: AgentRequest) -> AgentSession:
        raise AgentAdapterError("Kimi resume is disabled with the restricted agent file")

    async def _consume(self, state: _KimiSessionState) -> AgentResult:
        self._emit(state, AgentEventType.STARTED)
        try:
            async for chunk in state.process.stream():
                if chunk.stream is ProcessStream.STDERR:
                    self._emit(state, AgentEventType.STDERR, text=chunk.text)
                elif not self._consume_stdout(state, chunk):
                    await state.process.cancel()
                    break
            process_result = await state.process.wait()
            try:
                await asyncio.to_thread(verify_artifact_files, state.artifact_inputs)
            except AgentAdapterError:
                state.error = "Artifact input changed during Kimi turn"
            result = self._build_result(state, process_result)
        except Exception as exc:  # noqa: BLE001 - close the event stream at the adapter boundary.
            state.session.status = AgentSessionStatus.FAILED
            self._emit(state, AgentEventType.FAILED, text=str(exc))
            result = AgentResult(
                session_id=state.session.session_id,
                trace_id=state.session.trace_id,
                reason=AgentExitReason.FAILED,
                duration_ms=0,
                error=str(exc),
            )
        state.queue.put_nowait(_END_OF_EVENTS)
        return result

    def _consume_stdout(self, state: _KimiSessionState, chunk: ProcessChunk) -> bool:
        try:
            payload = json.loads(chunk.text)
        except json.JSONDecodeError:
            state.error = "Kimi CLI returned invalid stream-json output"
            return False
        if not isinstance(payload, dict):
            state.error = "Kimi CLI returned a non-object stream-json event"
            return False
        role = payload.get("role")
        if role == "assistant":
            calls = payload.get("tool_calls") or []
            if not isinstance(calls, list):
                state.error = "Kimi CLI returned malformed tool calls"
                return False
            for call in calls:
                name = self._tool_name(call)
                if name not in _ALLOWED_TOOLS:
                    state.error = f"Kimi CLI attempted a disallowed tool: {name or 'unknown'}"
                    return False
                data = {"name": name}
                # Keep only the file path, not arbitrary tool arguments or contents.
                function = call.get("function", call)
                arguments = function.get("arguments") if isinstance(function, dict) else None
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = None
                if isinstance(arguments, dict):
                    path = arguments.get("path", arguments.get("file_path"))
                    if isinstance(path, str) and 0 < len(path) <= 4096 and "\0" not in path:
                        data["path"] = path
                self._emit(
                    state,
                    AgentEventType.TOOL_CALL,
                    data=data,
                    native_event_type="assistant.tool_call",
                )
            content = payload.get("content")
            if isinstance(content, str) and content:
                state.last_message = content
                self._emit(
                    state, AgentEventType.MESSAGE, text=content, native_event_type="assistant"
                )
            return True
        if role == "tool":
            call_id = payload.get("tool_call_id")
            self._emit(
                state,
                AgentEventType.MESSAGE,
                data={"tool_call_id": call_id} if isinstance(call_id, str) else {},
                native_event_type="tool",
            )
            return True
        if role == "meta":
            meta_type = payload.get("type")
            if isinstance(meta_type, str) and meta_type in _INFORMATIONAL_META_TYPES:
                # These are CLI protocol notices, not model answers. Native
                # resume remains disabled with our restricted --agent-file.
                self._emit(
                    state,
                    AgentEventType.STDOUT,
                    data={"meta_type": meta_type},
                    native_event_type=f"meta.{meta_type}",
                )
                return True
            safe_type = (
                meta_type
                if isinstance(meta_type, str)
                and 0 < len(meta_type) <= 64
                and all(char.isascii() and (char.isalnum() or char in "._-") for char in meta_type)
                else "unknown"
            )
            state.error = f"Kimi CLI returned an unexpected stream-json meta type: {safe_type}"
            return False
        state.error = f"Kimi CLI returned an unexpected stream-json role: {role!r}"
        return False

    @staticmethod
    def _tool_name(call: Any) -> str | None:
        if not isinstance(call, dict):
            return None
        function = call.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            return function["name"]
        name = call.get("name")
        return name if isinstance(name, str) else None

    def _build_result(self, state: _KimiSessionState, process: ProcessResult) -> AgentResult:
        if state.error is not None:
            reason, status, event = (
                AgentExitReason.FAILED,
                AgentSessionStatus.FAILED,
                AgentEventType.FAILED,
            )
        elif process.cancelled:
            reason, status, event = (
                AgentExitReason.CANCELLED,
                AgentSessionStatus.CANCELLED,
                AgentEventType.CANCELLED,
            )
        elif process.timed_out:
            reason, status, event = (
                AgentExitReason.TIMED_OUT,
                AgentSessionStatus.TIMED_OUT,
                AgentEventType.FAILED,
            )
        elif process.exit_code != 0 or state.last_message is None:
            reason, status, event = (
                AgentExitReason.FAILED,
                AgentSessionStatus.FAILED,
                AgentEventType.FAILED,
            )
        else:
            reason, status, event = (
                AgentExitReason.COMPLETED,
                AgentSessionStatus.COMPLETED,
                AgentEventType.COMPLETED,
            )
        state.session.status = status
        self._emit(state, event, text=state.error)
        return AgentResult(
            session_id=state.session.session_id,
            trace_id=state.session.trace_id,
            reason=reason,
            exit_code=process.exit_code,
            duration_ms=process.duration_ms,
            output={"message": state.last_message} if state.last_message is not None else {},
            error=state.error if reason is AgentExitReason.FAILED else None,
        )

    def _emit(
        self,
        state: _KimiSessionState,
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

    def _get_state(self, session_id: UUID) -> _KimiSessionState:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise AgentSessionNotFoundError(f"unknown session: {session_id}") from exc
