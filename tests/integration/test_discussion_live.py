"""Opt-in, bounded three-model chat smoke; no coding workflow or raw secrets in output.

Run only with CODECREW_RUN_DISCUSSION_LIVE=1 and the Kimi/DeepSeek keys in the
same terminal environment. The test can spend up to six model turns.
"""

import asyncio
import os
import platform
import shutil
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.agents import (
    AgentRegistry,
    AgentRole,
    CodexCliAdapter,
    DeepSeekClaudeReviewerAdapter,
    KimiCodeAdapter,
    PermissionMode,
)
from app.config import Settings
from app.main import create_app
from app.team import MemberRole, MessageType
from app.team.turns import AgentTurnRunner
from app.workspace import PermissionPolicy

pytest_plugins = ("tests.test_continuation_workflow",)


def _resolve_cli_executables() -> dict[str, str]:
    settings = Settings()
    configured = {
        "codex": settings.codex_cli_path,
        "kimi": settings.kimi_cli_path,
        "claude": settings.claude_cli_path,
    }
    executables = {}
    for name, command in configured.items():
        executable = shutil.which(command)
        if executable is None:
            raise ValueError(
                f"{name} CLI is unavailable at {command!r}; set "
                f"CODECREW_{name.upper()}_CLI_PATH or update PATH"
            )
        executables[name] = executable
    return executables


def test_live_cli_preflight_uses_configured_paths(tmp_path, monkeypatch):
    configured = {}
    for name in ("codex", "kimi", "claude"):
        executable = tmp_path / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        monkeypatch.setenv(f"CODECREW_{name.upper()}_CLI_PATH", str(executable))
        configured[name] = str(executable)

    assert _resolve_cli_executables() == configured

    monkeypatch.setenv("CODECREW_CODEX_CLI_PATH", str(tmp_path / "missing-codex"))
    with pytest.raises(ValueError, match="CODECREW_CODEX_CLI_PATH"):
        _resolve_cli_executables()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("CODECREW_RUN_DISCUSSION_LIVE") != "1",
    reason="set CODECREW_RUN_DISCUSSION_LIVE=1 to spend up to six real model turns",
)
async def test_three_real_agents_discuss_via_http_without_code_change(waiting_for_planner):
    if platform.system() != "Darwin":
        pytest.fail("live three-agent chat requires macOS Seatbelt for Kimi")
    try:
        executables = _resolve_cli_executables()
    except ValueError as exc:
        pytest.fail(str(exc))
    for name in ("KIMI_MODEL_API_KEY", "DEEPSEEK_API_KEY"):
        if not os.environ.get(name, "").strip():
            pytest.fail(f"set {name} in this terminal; never put it in the command")

    service, task, _workflow_agents = waiting_for_planner
    room = (await service.get_room(task.task_id)).room
    original_task = await service.get_task(task.task_id)
    original_runtime = service.contexts.get(task.task_id)
    worktree = original_runtime.context.worktree.worktree_path
    registry = AgentRegistry()
    registry.register(
        CodexCliAdapter(executable=executables["codex"]),
        roles={AgentRole.PLANNER}, permission_modes={PermissionMode.READ_ONLY},
    )
    registry.register(
        KimiCodeAdapter(
            worktree_root=service.worktrees.root,
            runtime_root=service.worktrees.root.parent / "kimi-chat-runtime",
            policy=PermissionPolicy(allowed_paths=("src",)),
            executable=executables["kimi"],
        ),
        roles={AgentRole.IMPLEMENTER}, permission_modes={PermissionMode.READ_ONLY},
    )
    registry.register(
        DeepSeekClaudeReviewerAdapter(executable=executables["claude"]),
        roles={AgentRole.REVIEWER}, permission_modes={PermissionMode.READ_ONLY},
    )
    service.agent_names = {
        MemberRole.PLANNER: "codex-cli",
        MemberRole.IMPLEMENTER: "kimi-code-cli",
        MemberRole.REVIEWER: "deepseek-claude-reviewer",
    }
    service.discussion_turns = AgentTurnRunner(
        registry, service.router, timeout_seconds=360, planner_timeout_seconds=360,
    )
    app = create_app(task_service=service)
    base = f"/api/v1/tasks/{task.task_id}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"{base}/messages/discussion", json={
            "expected_revision": task.revision,
            "idempotency_key": str(uuid4()),
            "content": (
                "@白金 @月见 @鲸鲸 请各用一两句话讨论：给 Python 函数新增输入校验时，"
                "怎样兼顾兼容性和测试？这是只读聊天，请回复 Human；不要修改任何文件。"
            ),
        })
        assert response.status_code == 201, response.status_code
        receipt = response.json()
        assert receipt["discussion_queued"] is True
        assert receipt["execution_authorized"] is False
        await asyncio.wait_for(service.wait_for_discussion(task.task_id), timeout=2400)
        messages = (await client.get(f"{base}/messages")).json()["items"]

    roles = {item["sender_role"] for item in messages
             if item["type"] == "discussion"
             and item["correlation_id"] == receipt["message"]["correlation_id"]}
    assert {"planner", "implementer", "reviewer"}.issubset(roles), (
        f"missing live chat roles: {sorted({'planner', 'implementer', 'reviewer'} - roles)}"
    )
    assert await service.get_task(task.task_id) == original_task
    assert service.contexts.get(task.task_id) == original_runtime
    assert not any(stored.message.type in {
        MessageType.IMPLEMENTATION_READY, MessageType.REVIEW_APPROVED,
    } for stored in service.rooms.list_messages(room.room_id))
    status = await asyncio.create_subprocess_exec(
        "git", "status", "--porcelain", cwd=worktree,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _stderr = await status.communicate()
    assert status.returncode == 0
    assert not stdout.strip(), "read-only discussion changed the worktree"
