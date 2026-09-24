import json
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.agents import (
    AgentAdapterError,
    AgentEventType,
    AgentExitReason,
    AgentRequest,
    AgentRole,
    AgentSessionStatus,
    ClaudeCodeAdapter,
    DeepSeekClaudeReviewerAdapter,
    PermissionMode,
)
from app.agents.process import ProcessChunk, ProcessResult, ProcessStartError, ProcessStream


class StubProcess:
    def __init__(
        self,
        chunks: list[ProcessChunk],
        result: ProcessResult,
    ) -> None:
        self._chunks = chunks
        self._result = result
        self.cancelled = False

    async def stream(self) -> AsyncIterator[ProcessChunk]:
        for chunk in self._chunks:
            yield chunk

    async def wait(self) -> ProcessResult:
        return self._result

    async def cancel(self) -> None:
        self.cancelled = True


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


class FailingRunner:
    async def start(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
    ) -> Any:
        raise ProcessStartError("claude executable is unavailable")


def make_request(**updates: Any) -> AgentRequest:
    values = {
        "task_id": uuid4(),
        "trace_id": uuid4(),
        "role": AgentRole.PLANNER,
        "prompt": "Inspect the repository.",
        "working_directory": Path("/tmp/repository"),
        "timeout_seconds": 30,
    }
    values.update(updates)
    return AgentRequest(**values)


def json_chunk(payload: dict[str, Any]) -> ProcessChunk:
    return ProcessChunk(ProcessStream.STDOUT, json.dumps(payload) + "\n")


@pytest.mark.asyncio
async def test_maps_claude_stream_and_result() -> None:
    native_session_id = str(uuid4())
    process = StubProcess(
        [
            json_chunk({"type": "system", "subtype": "init", "session_id": native_session_id}),
            json_chunk(
                {
                    "type": "assistant",
                    "session_id": native_session_id,
                    "message": {
                        "content": [
                            {"type": "text", "text": "Plan ready"},
                            {"type": "tool_use", "name": "Read", "input": {"path": "a.py"}},
                        ]
                    },
                }
            ),
            ProcessChunk(ProcessStream.STDERR, "diagnostic\n"),
            json_chunk(
                {
                    "type": "result",
                    "subtype": "success",
                    "session_id": native_session_id,
                    "is_error": False,
                    "result": "Plan ready",
                    "duration_ms": 123,
                    "total_cost_usd": 0.01,
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_read_input_tokens": 3,
                    },
                }
            ),
        ],
        ProcessResult(exit_code=0, duration_ms=150),
    )
    runner = StubRunner(process)
    adapter = ClaudeCodeAdapter(runner=runner)

    session = await adapter.start(make_request())
    events = [event async for event in adapter.stream(session.session_id)]
    result = await adapter.wait(session.session_id)

    assert [event.type for event in events] == [
        AgentEventType.STARTED,
        AgentEventType.MESSAGE,
        AgentEventType.TOOL_CALL,
        AgentEventType.STDERR,
        AgentEventType.COMPLETED,
    ]
    assert [event.sequence for event in events] == list(range(5))
    assert session.native_session_id == native_session_id
    assert session.status is AgentSessionStatus.COMPLETED
    assert result.reason is AgentExitReason.COMPLETED
    assert result.duration_ms == 123
    assert result.output == {"result": "Plan ready", "total_cost_usd": 0.01}
    assert result.token_usage is not None
    assert result.token_usage.total_tokens == 15


def test_builds_read_only_non_shell_command_and_resume() -> None:
    process = StubProcess([], ProcessResult(exit_code=0, duration_ms=1))
    adapter = ClaudeCodeAdapter(executable="/usr/local/bin/claude", runner=StubRunner(process))
    request = make_request(resume_from_session_id="native-1", prompt="literal $(touch bad)")

    command = adapter.build_command(request)

    assert command[0] == "/usr/local/bin/claude"
    assert command[-1] == "literal $(touch bad)"
    assert "--safe-mode" in command
    assert "--strict-mcp-config" in command
    assert '--mcp-config={"mcpServers":{}}' in command
    assert "--disable-slash-commands" in command
    assert command[command.index("--permission-mode") + 1] == "plan"
    assert "--tools=Read,Glob,Grep" in command
    assert command[command.index("--resume") + 1] == "native-1"


@pytest.mark.asyncio
async def test_rejects_write_permission_before_starting_process() -> None:
    process = StubProcess([], ProcessResult(exit_code=0, duration_ms=1))
    runner = StubRunner(process)
    adapter = ClaudeCodeAdapter(runner=runner)

    with pytest.raises(AgentAdapterError, match="read-only"):
        await adapter.start(make_request(permission_mode=PermissionMode.WORKSPACE_WRITE))

    assert runner.calls == []


@pytest.mark.asyncio
async def test_normalizes_process_start_failure() -> None:
    adapter = ClaudeCodeAdapter(runner=FailingRunner())

    with pytest.raises(AgentAdapterError, match="claude executable is unavailable"):
        await adapter.start(make_request())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("process_result", "payload", "reason", "status"),
    [
        (
            ProcessResult(exit_code=1, duration_ms=10),
            {"type": "result", "is_error": True, "result": "provider error"},
            AgentExitReason.FAILED,
            AgentSessionStatus.FAILED,
        ),
        (
            ProcessResult(exit_code=-15, duration_ms=10, timed_out=True),
            {},
            AgentExitReason.TIMED_OUT,
            AgentSessionStatus.TIMED_OUT,
        ),
        (
            ProcessResult(exit_code=-15, duration_ms=10, cancelled=True),
            {},
            AgentExitReason.CANCELLED,
            AgentSessionStatus.CANCELLED,
        ),
    ],
)
async def test_maps_unsuccessful_process_outcomes(
    process_result: ProcessResult,
    payload: dict[str, Any],
    reason: AgentExitReason,
    status: AgentSessionStatus,
) -> None:
    chunks = [json_chunk(payload)] if payload else []
    adapter = ClaudeCodeAdapter(runner=StubRunner(StubProcess(chunks, process_result)))

    session = await adapter.start(make_request())
    result = await adapter.wait(session.session_id)

    assert result.reason is reason
    assert session.status is status


@pytest.mark.asyncio
async def test_preserves_malformed_stdout_as_text_event() -> None:
    process = StubProcess(
        [ProcessChunk(ProcessStream.STDOUT, "not-json\n")],
        ProcessResult(exit_code=0, duration_ms=1),
    )
    adapter = ClaudeCodeAdapter(runner=StubRunner(process))

    session = await adapter.start(make_request())
    events = [event async for event in adapter.stream(session.session_id)]

    assert events[1].type is AgentEventType.STDOUT
    assert events[1].text == "not-json\n"


@pytest.mark.asyncio
async def test_resume_adds_native_session_to_command() -> None:
    process = StubProcess([], ProcessResult(exit_code=0, duration_ms=1))
    runner = StubRunner(process)
    adapter = ClaudeCodeAdapter(runner=runner)

    session = await adapter.resume("native-42", make_request())
    await adapter.wait(session.session_id)

    assert runner.calls[0]["argv"][-3:-1] == ["--resume", "native-42"]


@pytest.mark.asyncio
async def test_deepseek_reviewer_uses_dedicated_read_only_environment() -> None:
    runner = StubRunner(StubProcess([], ProcessResult(exit_code=0, duration_ms=1)))
    adapter = DeepSeekClaudeReviewerAdapter(
        runner=runner,
        env_source={
            "DEEPSEEK_API_KEY": "test-key",
            "MOONSHOT_API_KEY": "must-not-leak",
            "ANTHROPIC_API_KEY": "wrong-provider",
            "PATH": "/usr/bin",
            "HOME": "/tmp/test-home",
        },
    )
    request = make_request(role=AgentRole.REVIEWER)

    session = await adapter.start(request)
    result = await adapter.wait(session.session_id)

    assert session.agent_name == "deepseek-claude-reviewer"
    assert result.reason is AgentExitReason.COMPLETED
    argv = runner.calls[0]["argv"]
    assert "--tools=Read,Glob,Grep" in argv
    assert "--safe-mode" in argv
    assert "--disable-slash-commands" in argv
    assert "--strict-mcp-config" in argv
    assert '--mcp-config={"mcpServers":{}}' in argv
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert "--resume" not in argv
    assert "test-key" not in " ".join(argv)
    environment = runner.calls[0]["env"]
    assert environment["ANTHROPIC_AUTH_TOKEN"] == "test-key"
    assert environment["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert environment["ANTHROPIC_MODEL"] == "deepseek-flash[1m]"
    assert environment["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "deepseek-flash[1m]"
    assert environment["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "deepseek-flash[1m]"
    assert environment["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "deepseek-flash"
    assert environment["CLAUDE_CODE_SUBAGENT_MODEL"] == "deepseek-flash"
    assert environment["PATH"] == "/usr/bin"
    assert "MOONSHOT_API_KEY" not in environment
    assert "ANTHROPIC_API_KEY" not in environment
    assert "DEEPSEEK_API_KEY" not in environment


@pytest.mark.asyncio
async def test_deepseek_reviewer_fails_closed_on_missing_key_or_wrong_role() -> None:
    runner = StubRunner(StubProcess([], ProcessResult(exit_code=0, duration_ms=1)))
    adapter = DeepSeekClaudeReviewerAdapter(runner=runner, env_source={})

    with pytest.raises(AgentAdapterError, match="reviewer requests only"):
        await adapter.start(make_request(role=AgentRole.PLANNER))
    with pytest.raises(AgentAdapterError, match="DEEPSEEK_API_KEY"):
        await adapter.start(make_request(role=AgentRole.REVIEWER))
    assert runner.calls == []
