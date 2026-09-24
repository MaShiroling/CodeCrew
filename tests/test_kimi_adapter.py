import json
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

import app.storage  # noqa: F401 - initialize the existing workspace import graph.
from app.agents import (
    AgentAdapterError,
    AgentCapability,
    AgentEventType,
    AgentExitReason,
    AgentRequest,
    AgentRole,
    KimiCodeAdapter,
    PermissionMode,
)
from app.agents.process import ProcessChunk, ProcessResult, ProcessStream
from app.workspace.permissions import PermissionPolicy


class StubProcess:
    def __init__(self, chunks: list[ProcessChunk], result: ProcessResult) -> None:
        self.chunks = chunks
        self.result = result
        self.cancelled = False

    async def stream(self) -> AsyncIterator[ProcessChunk]:
        for chunk in self.chunks:
            yield chunk

    async def wait(self) -> ProcessResult:
        return self.result

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
        self.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout_seconds, "env": env})
        return self.process


class StubBoundary:
    def __init__(self, **kwargs: Any) -> None:
        self.options = kwargs

    def wrap(self, argv: Sequence[str]) -> list[str]:
        return ["sandbox-exec", "-p", "restricted", *argv]


def make_adapter(tmp_path: Path, process: StubProcess, *, key: str = "test-key") -> tuple[KimiCodeAdapter, StubRunner, AgentRequest]:
    task_id = uuid4()
    worktree_root = tmp_path / "worktrees"
    worktree = worktree_root / str(task_id)
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: test\n")
    (worktree / "src").mkdir()
    runner = StubRunner(process)
    adapter = KimiCodeAdapter(
        worktree_root=worktree_root,
        runtime_root=tmp_path / "kimi-runtime",
        policy=PermissionPolicy(allowed_paths=("src",)),
        runner=runner,
        env_source={"KIMI_MODEL_API_KEY": key, "PATH": "/usr/bin", "ANTHROPIC_API_KEY": "wrong"},
        boundary_factory=StubBoundary,
    )
    request = AgentRequest(
        task_id=task_id,
        trace_id=uuid4(),
        role=AgentRole.IMPLEMENTER,
        prompt="Implement the plan",
        working_directory=worktree,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        timeout_seconds=30,
    )
    return adapter, runner, request


def json_chunk(payload: dict[str, Any]) -> ProcessChunk:
    return ProcessChunk(ProcessStream.STDOUT, json.dumps(payload) + "\n")


@pytest.mark.asyncio
async def test_kimi_lifecycle_uses_fresh_sandboxed_session_and_isolated_environment(tmp_path: Path) -> None:
    process = StubProcess(
        [
            json_chunk({"role": "assistant", "tool_calls": [{"function": {"name": "Edit"}}]}),
            json_chunk({"role": "tool", "content": "edited"}),
            json_chunk({"role": "assistant", "content": '{"actions": []}'}),
        ],
        ProcessResult(exit_code=0, duration_ms=12),
    )
    adapter, runner, request = make_adapter(tmp_path, process)

    session = await adapter.start(request)
    events = [event async for event in adapter.stream(session.session_id)]
    result = await adapter.wait(session.session_id)

    assert result.reason is AgentExitReason.COMPLETED
    assert result.output == {"message": '{"actions": []}'}
    assert result.token_usage is None
    assert session.native_session_id is None
    assert AgentCapability.SESSION_RESUME not in adapter.capabilities
    assert [event.type for event in events] == [
        AgentEventType.STARTED, AgentEventType.TOOL_CALL, AgentEventType.MESSAGE,
        AgentEventType.MESSAGE, AgentEventType.COMPLETED,
    ]
    command = runner.calls[0]["argv"]
    assert command[:3] == ["sandbox-exec", "-p", "restricted"]
    assert "--agent-file" in command and "--output-format" in command
    assert "--yolo" not in command and "--auto" not in command
    assert "test-key" not in " ".join(command)
    env = runner.calls[0]["env"]
    assert env["KIMI_MODEL_NAME"] == "kimi-k3"
    assert env["KIMI_MODEL_API_KEY"] == "test-key"
    assert env["KIMI_DISABLE_CRON"] == "1"
    assert "ANTHROPIC_API_KEY" not in env
    assert str(session.session_id) in env["KIMI_CODE_HOME"]
    assert runner.calls[0]["cwd"] == request.working_directory


@pytest.mark.asyncio
async def test_kimi_fails_on_unexpected_tool_and_invalid_json(tmp_path: Path) -> None:
    for chunk in (
        json_chunk({"role": "assistant", "tool_calls": [{"function": {"name": "Bash"}}]}),
        ProcessChunk(ProcessStream.STDOUT, "not-json\n"),
    ):
        process = StubProcess([chunk], ProcessResult(exit_code=0, duration_ms=1))
        adapter, _, request = make_adapter(tmp_path / str(uuid4()), process)
        session = await adapter.start(request)
        result = await adapter.wait(session.session_id)
        assert result.reason is AgentExitReason.FAILED
        assert process.cancelled


@pytest.mark.asyncio
async def test_kimi_rejects_wrong_role_key_worktree_and_resume(tmp_path: Path) -> None:
    process = StubProcess([], ProcessResult(exit_code=0, duration_ms=1))
    adapter, runner, request = make_adapter(tmp_path, process, key="")
    with pytest.raises(AgentAdapterError, match="implementer"):
        await adapter.start(request.model_copy(update={"role": AgentRole.REVIEWER}))
    with pytest.raises(AgentAdapterError, match="KIMI_MODEL_API_KEY"):
        await adapter.start(request)
    assert runner.calls == []

    adapter, runner, request = make_adapter(tmp_path / "second", process)
    with pytest.raises(AgentAdapterError, match="worktree"):
        await adapter.start(request.model_copy(update={"working_directory": tmp_path}))
    with pytest.raises(AgentAdapterError, match="resume is disabled"):
        await adapter.resume("native-id", request)
    assert runner.calls == []


@pytest.mark.asyncio
async def test_kimi_rejects_modified_tool_profile_before_process_start(tmp_path: Path) -> None:
    process = StubProcess([], ProcessResult(exit_code=0, duration_ms=1))
    adapter, runner, request = make_adapter(tmp_path, process)
    modified = tmp_path / "unsafe-agent.md"
    modified.write_text(
        adapter._agent_file.read_text(encoding="utf-8").replace(
            "  - Edit\n", "  - Edit\n  - Bash\n"
        ),
        encoding="utf-8",
    )
    adapter._agent_file = modified

    with pytest.raises(AgentAdapterError, match="changed unexpectedly"):
        await adapter.start(request)
    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("process_result", "reason"),
    [
        (ProcessResult(exit_code=-15, duration_ms=10, cancelled=True), AgentExitReason.CANCELLED),
        (ProcessResult(exit_code=-15, duration_ms=10, timed_out=True), AgentExitReason.TIMED_OUT),
    ],
)
async def test_kimi_maps_cancellation_and_timeout(
    tmp_path: Path, process_result: ProcessResult, reason: AgentExitReason
) -> None:
    adapter, _, request = make_adapter(tmp_path, StubProcess([], process_result))
    session = await adapter.start(request)
    result = await adapter.wait(session.session_id)
    assert result.reason is reason
