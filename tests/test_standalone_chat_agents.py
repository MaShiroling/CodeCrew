"""No-repository CLI launch contexts; never dispatch from a room message here."""

import json
import os
import platform
import shutil
import subprocess
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import pytest

from app.agents import (
    AgentAdapterError,
    AgentEventType,
    AgentExitReason,
    AgentRequest,
    AgentRole,
    CodexCliAdapter,
    DeepSeekClaudeReviewerAdapter,
    KimiCodeAdapter,
    PermissionMode,
)
from app.agents.kimi_boundary import KimiWriteBoundary
from app.agents.process import ProcessChunk, ProcessResult, ProcessStream
from app.chat.agents import (
    StandaloneChatAgentError,
    StandaloneChatAgentRuntime,
    StandaloneChatWorkspaceManager,
    build_standalone_chat_agent_runtime,
)
from app.config import Settings
from app.team.models import MemberRole
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
        self, argv: Sequence[str], *, cwd: Path, timeout_seconds: float,
        env: Mapping[str, str] | None = None,
    ) -> StubProcess:
        self.calls.append({"argv": list(argv), "cwd": cwd,
                           "timeout_seconds": timeout_seconds, "env": env})
        return self.process


class StubBoundary:
    calls: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    def wrap(self, argv: Sequence[str]) -> list[str]:
        return ["sandbox-exec", "-p", "readonly", *argv]


def json_chunk(payload: dict[str, Any]) -> ProcessChunk:
    return ProcessChunk(ProcessStream.STDOUT, json.dumps(payload) + "\n")


def make_workspaces(tmp_path: Path) -> StandaloneChatWorkspaceManager:
    return StandaloneChatWorkspaceManager(
        tmp_path / "chat-workspaces", tmp_path / "chat-runtime",
    )


def test_private_workspace_is_not_a_git_repository_and_can_be_cleaned(tmp_path: Path) -> None:
    manager = make_workspaces(tmp_path)
    handle = manager.create(uuid4())
    assert handle.path.is_dir() and not (handle.path / ".git").exists()
    assert handle.runtime.joinpath("home").is_dir()
    assert handle.runtime.joinpath("tmp").is_dir()
    assert handle.path.stat().st_mode & 0o077 == 0
    manager.cleanup(handle)
    manager.cleanup(handle)
    assert not handle.path.exists() and not handle.runtime.exists()


def test_workspace_rejects_git_parent_symlink_and_public_root(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    with pytest.raises(StandaloneChatAgentError, match="outside Git"):
        StandaloneChatWorkspaceManager(checkout / "chat", tmp_path / "runtime")

    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(StandaloneChatAgentError, match="symlink"):
        StandaloneChatWorkspaceManager(link / "chat", tmp_path / "runtime2")

    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    public.chmod(0o755)
    with pytest.raises(StandaloneChatAgentError, match="private"):
        StandaloneChatWorkspaceManager(public, tmp_path / "runtime3")


def test_request_requires_fresh_readonly_discussion_and_private_runtime(tmp_path: Path) -> None:
    values = {
        "task_id": uuid4(), "trace_id": uuid4(), "role": AgentRole.IMPLEMENTER,
        "prompt": "Discuss", "working_directory": tmp_path / "work",
        "standalone_chat_room_id": uuid4(), "runtime_directory": tmp_path / "runtime",
        "discussion_only": True, "permission_mode": PermissionMode.READ_ONLY,
    }
    assert AgentRequest(**values).standalone_chat_room_id is not None
    for change in (
        {"discussion_only": False},
        {"permission_mode": PermissionMode.WORKSPACE_WRITE},
        {"resume_from_session_id": "old"},
        {"runtime_directory": None},
        {"runtime_directory": tmp_path / "work"},
    ):
        with pytest.raises(ValueError, match="standalone chat requires|read-only permission"):
            AgentRequest(**{**values, **change})


def test_readonly_kimi_boundary_needs_no_task_write_directory(tmp_path: Path) -> None:
    workspace, runtime = tmp_path / "empty", tmp_path / "runtime"
    workspace.mkdir()
    runtime.mkdir()
    boundary = KimiWriteBoundary(
        worktree=workspace, runtime_directory=runtime,
        policy=PermissionPolicy(allowed_paths=("src",)), worktree_read_only=True,
    )
    profile = boundary.profile()
    assert boundary.allowed_directories == ()
    assert f'(deny file-write* (subpath "{workspace}"))' in profile
    assert f'(allow file-write* (subpath "{workspace}"))' not in profile
    assert f'(allow file-write* (subpath "{runtime}"))' in profile


@pytest.mark.skipif(
    platform.system() != "Darwin" or shutil.which("kimi") is None,
    reason="requires macOS Seatbelt and an installed Kimi CLI",
)
def test_kimi_binary_starts_without_git_or_model_call(tmp_path: Path) -> None:
    manager = make_workspaces(tmp_path)
    handle = manager.create(uuid4())
    binary = Path(shutil.which("kimi"))
    boundary = KimiWriteBoundary(
        worktree=handle.path, runtime_directory=handle.runtime,
        policy=PermissionPolicy(allowed_paths=("src",)), worktree_read_only=True,
        readable_files=(binary,),
    )
    result = subprocess.run(
        boundary.wrap([str(binary), "--version"]),
        cwd=handle.path,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin"),
            "HOME": str(handle.runtime / "home"),
            "KIMI_CODE_HOME": str(handle.runtime),
            "TMPDIR": str(handle.runtime / "tmp"),
        },
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()
    manager.cleanup(handle)


@pytest.mark.asyncio
async def test_kimi_only_skips_git_check_for_explicit_standalone_chat(tmp_path: Path) -> None:
    manager = make_workspaces(tmp_path)
    handle = manager.create(uuid4())
    runner = StubRunner(StubProcess([], ProcessResult(exit_code=0, duration_ms=1)))
    adapter = KimiCodeAdapter(
        worktree_root=manager.workspace_root, runtime_root=manager.runtime_root,
        policy=PermissionPolicy(allowed_paths=("missing-task-directory",)),
        runner=runner, boundary_factory=StubBoundary,
        env_source={"KIMI_MODEL_API_KEY": "test", "PATH": "/usr/bin"},
        allow_standalone_chat=True,
    )
    request = AgentRequest(
        task_id=handle.execution_id, trace_id=uuid4(), role=AgentRole.IMPLEMENTER,
        prompt="Discuss", working_directory=handle.path,
        permission_mode=PermissionMode.READ_ONLY, discussion_only=True,
    )
    with pytest.raises(AgentAdapterError, match="Git worktree"):
        await adapter.start(request)
    (handle.path / ".git").mkdir()
    standalone = request.model_copy(update={
        "standalone_chat_room_id": handle.room_id,
        "runtime_directory": handle.runtime,
    })
    with pytest.raises(AgentAdapterError, match="cannot run in a Git worktree"):
        await adapter.start(standalone)
    assert runner.calls == []
    manager.cleanup(handle)


@pytest.mark.asyncio
async def test_adapters_revalidate_copied_chat_requests_before_process_launch(tmp_path: Path) -> None:
    manager = make_workspaces(tmp_path)
    handle = manager.create(uuid4())
    valid = AgentRequest(
        task_id=handle.execution_id, trace_id=uuid4(), role=AgentRole.IMPLEMENTER,
        prompt="Discuss", working_directory=handle.path,
        runtime_directory=handle.runtime, standalone_chat_room_id=handle.room_id,
        permission_mode=PermissionMode.READ_ONLY, discussion_only=True,
    )
    runner = StubRunner(StubProcess([], ProcessResult(exit_code=0, duration_ms=1)))
    legacy_kimi = KimiCodeAdapter(
        worktree_root=manager.workspace_root, runtime_root=manager.runtime_root,
        policy=PermissionPolicy(allowed_paths=(".",)), runner=runner,
        boundary_factory=StubBoundary,
        env_source={"KIMI_MODEL_API_KEY": "test", "PATH": "/usr/bin"},
    )
    with pytest.raises(AgentAdapterError, match="not bound to standalone chat"):
        await legacy_kimi.start(valid)

    kimi = KimiCodeAdapter(
        worktree_root=manager.workspace_root, runtime_root=manager.runtime_root,
        policy=PermissionPolicy(allowed_paths=(".",)), runner=runner,
        boundary_factory=StubBoundary, allow_standalone_chat=True,
        env_source={"KIMI_MODEL_API_KEY": "test", "PATH": "/usr/bin"},
    )
    for adapter in (
        CodexCliAdapter(runner=runner),
        kimi,
        DeepSeekClaudeReviewerAdapter(
            runner=runner, env_source={"DEEPSEEK_API_KEY": "test", "PATH": "/usr/bin"},
        ),
    ):
        tampered = valid.model_copy(update={
            "permission_mode": PermissionMode.WORKSPACE_WRITE,
            "role": AgentRole.REVIEWER if isinstance(
                adapter, DeepSeekClaudeReviewerAdapter
            ) else AgentRole.IMPLEMENTER,
        })
        with pytest.raises(AgentAdapterError, match="invalid standalone chat request"):
            await adapter.start(tampered)
    assert runner.calls == []
    manager.cleanup(handle)


@pytest.mark.asyncio
async def test_three_real_adapter_contracts_use_private_no_git_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KIMI_MODEL_API_KEY", "kimi-test-only")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-only")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "wrong-provider")
    monkeypatch.setenv("CODECREW_RANDOM_SECRET", "must-not-leak")
    StubBoundary.calls = []
    manager = make_workspaces(tmp_path)
    codex_runner = StubRunner(StubProcess([
        json_chunk({"type": "thread.started", "thread_id": "codex-native"}),
        json_chunk({"type": "item.completed", "item": {
            "type": "agent_message", "text": "Planner reply",
        }}),
        json_chunk({"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 3}}),
    ], ProcessResult(exit_code=0, duration_ms=12)))
    kimi_runner = StubRunner(StubProcess([
        json_chunk({"role": "assistant", "content": "Implementer reply"}),
    ], ProcessResult(exit_code=0, duration_ms=12)))
    reviewer_runner = StubRunner(StubProcess([
        json_chunk({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Reviewer reply"},
        ]}}),
        json_chunk({"type": "result", "subtype": "success", "result": "Reviewer reply",
                    "session_id": "review-native"}),
    ], ProcessResult(exit_code=0, duration_ms=12)))
    runtime = StandaloneChatAgentRuntime(manager, {
        MemberRole.PLANNER: CodexCliAdapter(runner=codex_runner),
        MemberRole.IMPLEMENTER: KimiCodeAdapter(
            worktree_root=manager.workspace_root, runtime_root=manager.runtime_root,
            policy=PermissionPolicy(allowed_paths=("src",)),
            runner=kimi_runner, boundary_factory=StubBoundary,
            env_source={"KIMI_MODEL_API_KEY": "kimi-test-only", "PATH": "/usr/bin"},
            allow_standalone_chat=True,
        ),
        MemberRole.REVIEWER: DeepSeekClaudeReviewerAdapter(
            runner=reviewer_runner,
            env_source={"DEEPSEEK_API_KEY": "deepseek-test-only", "PATH": "/usr/bin"},
        ),
    })
    room_id, trace_id = uuid4(), uuid4()
    for role, runner in (
        (MemberRole.PLANNER, codex_runner),
        (MemberRole.IMPLEMENTER, kimi_runner),
        (MemberRole.REVIEWER, reviewer_runner),
    ):
        session = await runtime.start(
            room_id=room_id, trace_id=trace_id, role=role,
            prompt="Only discuss; no code changes", timeout_seconds=30,
        )
        cwd = runner.calls[0]["cwd"]
        assert cwd.parent == manager.workspace_root and not (cwd / ".git").exists()
        events = [event async for event in runtime.stream(session.session_id)]
        result = await runtime.wait(session.session_id)
        assert result.reason is AgentExitReason.COMPLETED
        assert any(event.type is AgentEventType.MESSAGE for event in events)
        assert not cwd.exists()

    codex = codex_runner.calls[0]
    assert "--skip-git-repo-check" in codex["argv"]
    assert codex["argv"][codex["argv"].index("--sandbox") + 1] == "read-only"
    assert "KIMI_MODEL_API_KEY" not in codex["env"]
    assert "DEEPSEEK_API_KEY" not in codex["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in codex["env"]
    assert "CODECREW_RANDOM_SECRET" not in codex["env"]

    kimi = kimi_runner.calls[0]
    assert kimi["argv"][0] == "sandbox-exec"
    assert "kimi_standalone_chat.md" in " ".join(kimi["argv"])
    assert StubBoundary.calls[0]["worktree_read_only"] is True
    assert kimi["env"]["KIMI_MODEL_API_KEY"] == "kimi-test-only"
    assert "DEEPSEEK_API_KEY" not in kimi["env"]

    reviewer = reviewer_runner.calls[0]
    assert "--safe-mode" in reviewer["argv"]
    assert "--tools=Read,Glob,Grep" in reviewer["argv"]
    assert reviewer["env"]["HOME"].startswith(str(manager.runtime_root))
    assert reviewer["env"]["TMPDIR"].startswith(str(manager.runtime_root))
    assert "KIMI_MODEL_API_KEY" not in reviewer["env"]


@pytest.mark.asyncio
async def test_cancel_and_timeout_close_private_session(tmp_path: Path) -> None:
    manager = make_workspaces(tmp_path)
    cancelled_process = StubProcess([], ProcessResult(
        exit_code=143, duration_ms=1, cancelled=True,
    ))
    timed_out_process = StubProcess([], ProcessResult(
        exit_code=143, duration_ms=30, timed_out=True,
    ))
    codex_runner = StubRunner(cancelled_process)
    runtime = StandaloneChatAgentRuntime(manager, {
        MemberRole.PLANNER: CodexCliAdapter(runner=codex_runner),
        MemberRole.IMPLEMENTER: CodexCliAdapter(runner=StubRunner(timed_out_process)),
        MemberRole.REVIEWER: CodexCliAdapter(runner=StubRunner(timed_out_process)),
    })
    session = await runtime.start(
        room_id=uuid4(), trace_id=uuid4(), role=MemberRole.PLANNER,
        prompt="Discuss", timeout_seconds=1,
    )
    cwd = codex_runner.calls[0]["cwd"]
    result = await runtime.cancel(session.session_id)
    assert cancelled_process.cancelled
    assert result.reason is AgentExitReason.CANCELLED
    assert await runtime.wait(session.session_id) == result
    assert await runtime.cancel(session.session_id) == result
    assert not cwd.exists()

    session2 = await runtime.start(
        room_id=uuid4(), trace_id=uuid4(), role=MemberRole.IMPLEMENTER,
        prompt="Discuss", timeout_seconds=1,
    )
    result2 = await runtime.wait(session2.session_id)
    assert result2.reason is AgentExitReason.TIMED_OUT


def test_builder_constructs_three_bindings_without_task_runtime(tmp_path: Path) -> None:
    settings = Settings(
        standalone_chat_workspace_root=tmp_path / "workspaces",
        standalone_chat_runtime_root=tmp_path / "runtime",
    )
    runtime = build_standalone_chat_agent_runtime(settings)
    assert isinstance(runtime.adapters[MemberRole.PLANNER], CodexCliAdapter)
    assert isinstance(runtime.adapters[MemberRole.IMPLEMENTER], KimiCodeAdapter)
    assert isinstance(runtime.adapters[MemberRole.REVIEWER], DeepSeekClaudeReviewerAdapter)
    assert not (tmp_path / "codecrew.db").exists()
