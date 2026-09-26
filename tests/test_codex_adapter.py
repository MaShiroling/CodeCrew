import json
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.agents import (
    AgentEventType,
    AgentExitReason,
    AgentRequest,
    AgentRole,
    AgentSessionStatus,
    CodexCliAdapter,
    PermissionMode,
)
from app.agents.process import ProcessChunk, ProcessResult, ProcessStream


class StubProcess:
    def __init__(self, chunks: list[ProcessChunk], result: ProcessResult) -> None:
        self._chunks = chunks
        self._result = result

    async def stream(self) -> AsyncIterator[ProcessChunk]:
        for chunk in self._chunks:
            yield chunk

    async def wait(self) -> ProcessResult:
        return self._result

    async def cancel(self) -> None:
        return None


class StubRunner:
    def __init__(self, process: StubProcess) -> None:
        self.process = process
        self.calls: list[dict[str, Any]] = []

    async def start(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
    ) -> Any:
        self.calls.append(
            {"argv": list(argv), "cwd": cwd, "timeout_seconds": timeout_seconds, "env": env}
        )
        return self.process


def make_request(**updates: Any) -> AgentRequest:
    values = {
        "task_id": uuid4(),
        "trace_id": uuid4(),
        "role": AgentRole.IMPLEMENTER,
        "prompt": "Implement the approved plan.",
        "working_directory": Path("/tmp/worktree"),
        "permission_mode": PermissionMode.WORKSPACE_WRITE,
        "timeout_seconds": 30,
    }
    values.update(updates)
    return AgentRequest(**values)


def json_chunk(payload: dict[str, Any]) -> ProcessChunk:
    return ProcessChunk(ProcessStream.STDOUT, json.dumps(payload) + "\n")


@pytest.mark.asyncio
async def test_maps_codex_jsonl_lifecycle() -> None:
    thread_id = str(uuid4())
    process = StubProcess(
        [
            json_chunk({"type": "thread.started", "thread_id": thread_id}),
            json_chunk({"type": "turn.started"}),
            json_chunk(
                {
                    "type": "item.completed",
                    "item": {"id": "item-1", "type": "agent_message", "text": "Done"},
                }
            ),
            json_chunk(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 20,
                        "cached_input_tokens": 4,
                        "output_tokens": 6,
                    },
                }
            ),
        ],
        ProcessResult(exit_code=0, duration_ms=120),
    )
    adapter = CodexCliAdapter(runner=StubRunner(process))

    session = await adapter.start(make_request())
    events = [event async for event in adapter.stream(session.session_id)]
    result = await adapter.wait(session.session_id)

    assert [event.type for event in events] == [
        AgentEventType.STARTED,
        AgentEventType.MESSAGE,
        AgentEventType.COMPLETED,
    ]
    assert session.native_session_id == thread_id
    assert session.status is AgentSessionStatus.COMPLETED
    assert result.reason is AgentExitReason.COMPLETED
    assert result.output == {"message": "Done"}
    assert result.token_usage is not None
    assert result.token_usage.total_tokens == 26


@pytest.mark.parametrize(
    ("permission_mode", "sandbox"),
    [
        (PermissionMode.READ_ONLY, "read-only"),
        (PermissionMode.WORKSPACE_WRITE, "workspace-write"),
    ],
)
def test_builds_sandboxed_non_shell_command(
    permission_mode: PermissionMode,
    sandbox: str,
) -> None:
    adapter = CodexCliAdapter(executable="/usr/local/bin/codex")
    request = make_request(
        permission_mode=permission_mode,
        prompt="literal $(touch bad)",
    )

    command = adapter.build_command(request)

    assert command[0] == "/usr/local/bin/codex"
    assert command[command.index("--sandbox") + 1] == sandbox
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert command[command.index("--cd") + 1] == "/tmp/worktree"
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert command[-1] == "literal $(touch bad)"


def test_resume_uses_exec_resume_subcommand() -> None:
    command = CodexCliAdapter().build_command(make_request(resume_from_session_id="thread-42"))

    exec_index = command.index("exec")
    assert command[exec_index : exec_index + 3] == ["exec", "resume", "thread-42"]


@pytest.mark.asyncio
async def test_maps_tool_calls_stderr_and_failure() -> None:
    process = StubProcess(
        [
            json_chunk(
                {
                    "type": "item.started",
                    "item": {"id": "cmd-1", "type": "command_execution", "command": "pytest"},
                }
            ),
            ProcessChunk(ProcessStream.STDERR, "warning\n"),
            json_chunk({"type": "turn.failed", "error": {"message": "model failed"}}),
        ],
        ProcessResult(exit_code=1, duration_ms=50),
    )
    adapter = CodexCliAdapter(runner=StubRunner(process))

    session = await adapter.start(make_request())
    events = [event async for event in adapter.stream(session.session_id)]
    result = await adapter.wait(session.session_id)

    assert [event.type for event in events] == [
        AgentEventType.STARTED,
        AgentEventType.TOOL_CALL,
        AgentEventType.STDERR,
        AgentEventType.MESSAGE,
        AgentEventType.FAILED,
    ]
    assert result.reason is AgentExitReason.FAILED
    assert result.error == "model failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("process_result", "reason", "status"),
    [
        (
            ProcessResult(exit_code=-15, duration_ms=10, timed_out=True),
            AgentExitReason.TIMED_OUT,
            AgentSessionStatus.TIMED_OUT,
        ),
        (
            ProcessResult(exit_code=-15, duration_ms=10, cancelled=True),
            AgentExitReason.CANCELLED,
            AgentSessionStatus.CANCELLED,
        ),
    ],
)
async def test_maps_timeout_and_cancellation(
    process_result: ProcessResult,
    reason: AgentExitReason,
    status: AgentSessionStatus,
) -> None:
    adapter = CodexCliAdapter(runner=StubRunner(StubProcess([], process_result)))

    session = await adapter.start(make_request())
    result = await adapter.wait(session.session_id)

    assert result.reason is reason
    assert session.status is status


@pytest.mark.asyncio
async def test_preserves_non_json_stdout() -> None:
    process = StubProcess(
        [ProcessChunk(ProcessStream.STDOUT, "plain output\n")],
        ProcessResult(exit_code=0, duration_ms=1),
    )
    adapter = CodexCliAdapter(runner=StubRunner(process))

    session = await adapter.start(make_request())
    events = [event async for event in adapter.stream(session.session_id)]

    assert events[1].type is AgentEventType.STDOUT
    assert events[1].text == "plain output\n"


@pytest.mark.asyncio
async def test_reconnection_and_https_fallback_can_complete() -> None:
    reconnects = [
        {"type": "error", "message": f"Reconnecting... {attempt}/5 (request timed out)"}
        for attempt in range(2, 6)
    ]
    fallback = {
        "type": "item.completed",
        "item": {
            "id": "item_0",
            "type": "error",
            "message": "Falling back from WebSockets to HTTPS transport. request timed out",
        },
    }
    process = StubProcess(
        [
            json_chunk({"type": "thread.started", "thread_id": "recovered-thread"}),
            json_chunk({"type": "turn.started"}),
            ProcessChunk(ProcessStream.STDERR, "failed to refresh available models\n"),
            *[json_chunk(payload) for payload in reconnects],
            json_chunk(fallback),
            json_chunk(
                {
                    "type": "item.completed",
                    "item": {"id": "item_1", "type": "agent_message", "text": "CODECREW_CODEX_OK"},
                }
            ),
            json_chunk(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 14294,
                        "cached_input_tokens": 12160,
                        "output_tokens": 10,
                    },
                }
            ),
        ],
        ProcessResult(exit_code=0, duration_ms=120),
    )
    adapter = CodexCliAdapter(runner=StubRunner(process))
    session = await adapter.start(make_request())
    events = [event async for event in adapter.stream(session.session_id)]
    result = await adapter.wait(session.session_id)

    assert result.reason is AgentExitReason.COMPLETED
    assert result.error is None
    assert result.output == {"message": "CODECREW_CODEX_OK"}
    assert session.native_session_id == "recovered-thread"
    assert result.token_usage is not None
    assert result.token_usage.total_tokens == 14304
    assert result.token_usage.cached_input_tokens == 12160
    assert [event.data for event in events if event.native_event_type == "error"] == reconnects
    assert (
        next(event for event in events if event.native_event_type == "item.completed.error").data
        == fallback
    )
    assert events[-1].type is AgentEventType.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payloads",
    [
        [],
        [{"type": "item.completed", "item": {"type": "agent_message", "text": "Done"}}],
        [{"type": "error", "message": "Reconnecting... 5/5"}],
        [{"type": "item.completed", "item": {"type": "error", "message": "fallback"}}],
        [{"type": "turn.failed", "error": {"message": "terminal"}}, {"type": "turn.completed"}],
        [{"type": "turn.completed"}, {"type": "turn.failed", "error": {"message": "terminal"}}],
        [
            {"type": "turn.failed", "error": {"message": "terminal"}},
            {"type": "error", "message": "retry"},
            {"type": "turn.completed"},
        ],
        [{"type": "turn.completed"}, {"type": "error", "message": "late error"}],
    ],
)
async def test_incomplete_or_failed_stream_never_completes(payloads: list[dict[str, Any]]) -> None:
    adapter = CodexCliAdapter(
        runner=StubRunner(
            StubProcess(
                [json_chunk(payload) for payload in payloads],
                ProcessResult(exit_code=0, duration_ms=1),
            )
        )
    )
    session = await adapter.start(make_request())
    result = await adapter.wait(session.session_id)

    assert result.reason is AgentExitReason.FAILED
    assert session.status is AgentSessionStatus.FAILED
    assert result.error
    if any(payload.get("type") == "turn.failed" for payload in payloads):
        assert result.error == "terminal"
    elif not payloads or payloads[0].get("item", {}).get("type") == "agent_message":
        assert result.error == "Codex stream ended without turn.completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("process_result", "reason"),
    [
        (ProcessResult(exit_code=1, duration_ms=1), AgentExitReason.FAILED),
        (ProcessResult(exit_code=-15, duration_ms=1, timed_out=True), AgentExitReason.TIMED_OUT),
        (ProcessResult(exit_code=-15, duration_ms=1, cancelled=True), AgentExitReason.CANCELLED),
    ],
)
async def test_completion_does_not_override_process_failure(
    process_result: ProcessResult,
    reason: AgentExitReason,
) -> None:
    adapter = CodexCliAdapter(
        runner=StubRunner(
            StubProcess(
                [json_chunk({"type": "turn.completed"})],
                process_result,
            )
        )
    )
    session = await adapter.start(make_request())
    assert (await adapter.wait(session.session_id)).reason is reason
