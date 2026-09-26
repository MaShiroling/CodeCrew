import hashlib
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

import app.storage  # noqa: F401 - initialize the existing workspace import graph.
from app.agents import (
    AgentAdapterError,
    AgentArtifactInput,
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


def make_adapter(
    tmp_path: Path, process: StubProcess, *, key: str = "test-key"
) -> tuple[KimiCodeAdapter, StubRunner, AgentRequest]:
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


def clarification_request(request):
    return AgentRequest.model_validate({
        **request.model_dump(), "clarification_only": True, "permission_mode": PermissionMode.READ_ONLY,
    })


@pytest.mark.asyncio
async def test_kimi_clarification_uses_validated_readonly_profile_and_boundary(tmp_path):
    process = StubProcess(
        [json_chunk({"role": "assistant", "tool_calls": [{"function": {"name": "Read"}}]}),
         json_chunk({"role": "assistant", "content": "question"})],
        ProcessResult(exit_code=0, duration_ms=1),
    )
    adapter, runner, request = make_adapter(tmp_path, process)
    captured = []

    def boundary(**options):
        captured.append(options)
        return StubBoundary(**options)

    adapter._boundary_factory = boundary
    session = await adapter.start(clarification_request(request))
    result = await adapter.wait(session.session_id)
    assert result.reason is AgentExitReason.COMPLETED
    argv = runner.calls[0]["argv"]
    profile = Path(argv[argv.index("--agent-file") + 1])
    assert profile.name == "kimi_readonly_clarifier.md"
    frontmatter = profile.read_text().split("---", maxsplit=2)[1]
    assert "  - Write" not in frontmatter and "  - Edit" not in frontmatter
    assert captured[0]["worktree_read_only"] is True
    assert profile in captured[0]["readable_files"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["Write", "Edit", "Bash"])
async def test_kimi_clarification_rejects_write_or_command_tool_calls(tmp_path, tool):
    process = StubProcess(
        [json_chunk({"role": "assistant", "tool_calls": [{"function": {"name": tool}}]})],
        ProcessResult(exit_code=0, duration_ms=1),
    )
    adapter, _, request = make_adapter(tmp_path, process)
    session = await adapter.start(clarification_request(request))
    result = await adapter.wait(session.session_id)
    assert result.reason is AgentExitReason.FAILED and process.cancelled
    assert result.error == f"Kimi CLI attempted a disallowed tool: {tool}"


@pytest.mark.asyncio
async def test_kimi_rejects_tampered_clarification_profile_before_launch(tmp_path):
    adapter, runner, request = make_adapter(tmp_path, StubProcess([], ProcessResult(exit_code=0, duration_ms=1)))
    modified = tmp_path / "unsafe-clarifier.md"
    modified.write_text(adapter._clarification_file.read_text().replace("  - Glob\n", "  - Glob\n  - Edit\n"))
    adapter._clarification_file = modified
    with pytest.raises(AgentAdapterError, match="changed unexpectedly"):
        await adapter.start(clarification_request(request))
    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code,cancelled,timed_out,expected", [
    (1, False, False, AgentExitReason.FAILED),
    (0, False, False, AgentExitReason.COMPLETED),
    (143, True, False, AgentExitReason.CANCELLED),
    (143, False, True, AgentExitReason.TIMED_OUT),
])
async def test_step_exhaustion_diagnostic_does_not_override_execution_facts(
    tmp_path, exit_code, cancelled, timed_out, expected,
):
    stderr = "error: failed to run prompt: loop.max_steps_exceeded: Turn exceeded maxSteps=12. private-details\n"
    process = StubProcess(
        [json_chunk({"role": "assistant", "content": "intermediate prose"}),
         ProcessChunk(ProcessStream.STDERR, stderr[:42]), ProcessChunk(ProcessStream.STDERR, stderr[42:])],
        ProcessResult(exit_code=exit_code, duration_ms=1, cancelled=cancelled, timed_out=timed_out),
    )
    adapter, runner, request = make_adapter(tmp_path, process)
    session = await adapter.start(request)
    events = [event async for event in adapter.stream(session.session_id)]
    result = await adapter.wait(session.session_id)
    assert result.reason is expected
    if expected is AgentExitReason.FAILED:
        assert result.error == "loop.max_steps_exceeded: Kimi turn step budget exhausted"
        assert events[-1].text == result.error
    else:
        assert result.error is None
    assert "private-details" not in (result.error or "")
    assert "".join(e.text for e in events if e.type is AgentEventType.STDERR) == stderr
    assert len(runner.calls) == 1  # No retry or extra model turn.


@pytest.mark.asyncio
async def test_unknown_cli_exit_still_has_safe_diagnostic(tmp_path):
    process = StubProcess(
        [ProcessChunk(ProcessStream.STDERR, "secret diagnostic\n")],
        ProcessResult(exit_code=7, duration_ms=1),
    )
    adapter, _, request = make_adapter(tmp_path, process)
    session = await adapter.start(request)
    result = await adapter.wait(session.session_id)
    assert result.error == "Kimi CLI exited with code 7"


def test_kimi_step_budget_is_explicit_and_validated(tmp_path: Path) -> None:
    adapter, _, _ = make_adapter(
        tmp_path, StubProcess([], ProcessResult(exit_code=0, duration_ms=1))
    )
    assert "KIMI_LOOP_MAX_STEPS_PER_TURN" not in adapter.build_process_env(tmp_path, "key")
    adapter._max_steps_per_turn = 4
    assert adapter.build_process_env(tmp_path, "key")["KIMI_LOOP_MAX_STEPS_PER_TURN"] == "4"
    with pytest.raises(ValueError, match="max_steps_per_turn"):
        KimiCodeAdapter(
            worktree_root=tmp_path / "worktrees",
            runtime_root=tmp_path / "runtime",
            policy=PermissionPolicy(allowed_paths=("src",)),
            max_steps_per_turn=0,
        )


@pytest.mark.asyncio
async def test_kimi_lifecycle_uses_fresh_sandboxed_session_and_isolated_environment(
    tmp_path: Path,
) -> None:
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
        AgentEventType.STARTED,
        AgentEventType.TOOL_CALL,
        AgentEventType.MESSAGE,
        AgentEventType.MESSAGE,
        AgentEventType.COMPLETED,
    ]
    command = runner.calls[0]["argv"]
    assert command[:3] == ["sandbox-exec", "-p", "restricted"]
    assert "--agent-file" in command and "--output-format" in command
    agent_file = Path(command[command.index("--agent-file") + 1])
    instructions = agent_file.read_text(encoding="utf-8")
    assert 'top-level key "actions"' in instructions
    assert "persona expression inside an action's \"content\" field" in instructions
    assert 'send "ask_question" to the planner, then "finish_turn"' in instructions
    assert "For requests without a task-room action schema" in instructions
    assert command[command.index("--prompt") + 1] == request.prompt
    assert "--yolo" not in command and "--auto" not in command
    assert "test-key" not in " ".join(command)
    env = runner.calls[0]["env"]
    assert env["KIMI_MODEL_NAME"] == "kimi-for-coding"
    assert env["KIMI_MODEL_PROVIDER_TYPE"] == "kimi"
    assert env["KIMI_MODEL_BASE_URL"] == "https://api.kimi.com/coding/v1"
    assert env["KIMI_MODEL_BASE_URL"] != "https://api.moonshot.ai/v1"
    assert env["KIMI_MODEL_API_KEY"] == "test-key"
    assert env["KIMI_DISABLE_CRON"] == "1"
    assert env["KIMI_DISABLE_TELEMETRY"] == "1"
    assert env["KIMI_CODE_NO_AUTO_UPDATE"] == "1"
    assert "ANTHROPIC_API_KEY" not in env
    assert str(session.session_id) in env["KIMI_CODE_HOME"]
    assert runner.calls[0]["cwd"] == request.working_directory


@pytest.mark.asyncio
async def test_kimi_ignores_known_meta_events_without_treating_them_as_answers(
    tmp_path: Path,
) -> None:
    process = StubProcess(
        [
            json_chunk({"role": "meta", "type": "system.version", "version": "2.0.0"}),
            json_chunk(
                {
                    "role": "meta",
                    "type": "turn.step.retrying",
                    "failed_attempt": 1,
                    "next_attempt": 2,
                    "max_attempts": 3,
                    "delay_ms": 1000,
                }
            ),
            json_chunk(
                {
                    "role": "meta",
                    "type": "session.resume_hint",
                    "session_id": "private-native-id",
                    "content": "To resume this session",
                }
            ),
            json_chunk({"role": "assistant", "content": "Implemented"}),
        ],
        ProcessResult(exit_code=0, duration_ms=12),
    )
    adapter, _, request = make_adapter(tmp_path, process)

    session = await adapter.start(request)
    events = [event async for event in adapter.stream(session.session_id)]
    result = await adapter.wait(session.session_id)

    assert result.reason is AgentExitReason.COMPLETED
    assert result.output == {"message": "Implemented"}
    assert session.native_session_id is None
    assert "private-native-id" not in repr(events)
    assert [event.data["meta_type"] for event in events if event.type is AgentEventType.STDOUT] == [
        "system.version",
        "turn.step.retrying",
        "session.resume_hint",
    ]


@pytest.mark.asyncio
async def test_kimi_resume_hint_alone_is_not_success(tmp_path: Path) -> None:
    process = StubProcess(
        [json_chunk({"role": "meta", "type": "session.resume_hint", "content": "resume"})],
        ProcessResult(exit_code=0, duration_ms=1),
    )
    adapter, _, request = make_adapter(tmp_path, process)
    session = await adapter.start(request)
    result = await adapter.wait(session.session_id)
    assert result.reason is AgentExitReason.FAILED


@pytest.mark.asyncio
async def test_kimi_unknown_meta_event_still_fails_closed(tmp_path: Path) -> None:
    process = StubProcess(
        [json_chunk({"role": "meta", "type": "unknown", "content": "ignored?"})],
        ProcessResult(exit_code=0, duration_ms=1),
    )
    adapter, _, request = make_adapter(tmp_path, process)
    session = await adapter.start(request)
    result = await adapter.wait(session.session_id)
    assert result.reason is AgentExitReason.FAILED
    assert result.error == "Kimi CLI returned an unexpected stream-json meta type: unknown"
    assert process.cancelled


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


def input_grant(request: AgentRequest, path: Path, content: bytes) -> AgentArtifactInput:
    return AgentArtifactInput(
        artifact_id=uuid4(),
        task_id=request.task_id,
        trace_id=request.trace_id,
        path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )


@pytest.mark.asyncio
async def test_kimi_rejects_input_grant_overlapping_writable_worktree(tmp_path: Path) -> None:
    adapter, runner, request = make_adapter(
        tmp_path, StubProcess([], ProcessResult(exit_code=0, duration_ms=1))
    )
    path = request.working_directory / "src" / "plan.json"
    path.write_bytes(b"plan")
    request = request.model_copy(update={"artifact_inputs": (input_grant(request, path, b"plan"),)})
    with pytest.raises(AgentAdapterError, match="outside writable"):
        await adapter.start(request)
    assert runner.calls == []


@pytest.mark.asyncio
async def test_kimi_grants_one_artifact_and_records_read_path_not_other_arguments(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "plan.json"
    plan.write_bytes(b"plan")
    process = StubProcess(
        [
            json_chunk(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "Read",
                                "arguments": json.dumps(
                                    {"path": str(plan), "secret": "never-record"}
                                ),
                            }
                        }
                    ],
                }
            ),
            json_chunk({"role": "assistant", "content": "done"}),
        ],
        ProcessResult(exit_code=0, duration_ms=1),
    )
    adapter, _, request = make_adapter(tmp_path, process)
    boundaries = []

    def boundary(**options):
        boundaries.append(options)
        return StubBoundary(**options)

    adapter._boundary_factory = boundary
    request = request.model_copy(update={"artifact_inputs": (input_grant(request, plan, b"plan"),)})
    session = await adapter.start(request)
    events = [event async for event in adapter.stream(session.session_id)]
    result = await adapter.wait(session.session_id)
    assert result.reason is AgentExitReason.COMPLETED
    assert boundaries[0]["read_only_files"] == (plan,)
    calls = [event for event in events if event.type is AgentEventType.TOOL_CALL]
    assert calls[0].data == {"name": "Read", "path": str(plan)}
    assert "never-record" not in repr(events)


@pytest.mark.asyncio
async def test_kimi_rejects_tampered_input_before_launch_and_after_turn(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    plan.write_bytes(b"plan")
    process = StubProcess(
        [json_chunk({"role": "assistant", "content": "done"})],
        ProcessResult(exit_code=0, duration_ms=1),
    )
    adapter, runner, request = make_adapter(tmp_path, process)
    request = request.model_copy(update={"artifact_inputs": (input_grant(request, plan, b"plan"),)})
    plan.write_bytes(b"evil")
    with pytest.raises(AgentAdapterError, match="integrity"):
        await adapter.start(request)
    assert runner.calls == []
    plan.write_bytes(b"plan")
    original_stream = process.stream

    async def tampering_stream():
        plan.write_bytes(b"evil")
        async for chunk in original_stream():
            yield chunk

    process.stream = tampering_stream
    session = await adapter.start(request)
    result = await adapter.wait(session.session_id)
    assert result.reason is AgentExitReason.FAILED
    assert result.error == "Artifact input changed during Kimi turn"
