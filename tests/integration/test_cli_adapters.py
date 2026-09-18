import asyncio
import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from app.agents import (
    AgentExitReason,
    AgentRequest,
    AgentRole,
    ClaudeCodeAdapter,
    CodexCliAdapter,
    PermissionMode,
)
from app.config import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("CODECREW_RUN_CLI_INTEGRATION") != "1",
        reason="set CODECREW_RUN_CLI_INTEGRATION=1 to invoke authenticated CLIs",
    ),
]


def request_for(role: AgentRole, directory: Path) -> AgentRequest:
    return AgentRequest(
        task_id=uuid4(),
        trace_id=uuid4(),
        role=role,
        prompt="Reply with exactly CODECREW_OK. Do not use tools.",
        working_directory=directory,
        permission_mode=PermissionMode.READ_ONLY,
        timeout_seconds=60,
    )


@pytest.mark.asyncio
async def test_live_claude_read_only_session(tmp_path: Path) -> None:
    settings = Settings(_env_file=None)
    if shutil.which(settings.claude_cli_path) is None:
        pytest.skip(f"Claude CLI not found: {settings.claude_cli_path}")
    adapter = ClaudeCodeAdapter(executable=settings.claude_cli_path)

    session = await adapter.start(request_for(AgentRole.PLANNER, tmp_path))
    events = [event async for event in adapter.stream(session.session_id)]
    result = await adapter.wait(session.session_id)

    assert result.reason is AgentExitReason.COMPLETED
    assert session.native_session_id
    assert any(event.text == "CODECREW_OK" for event in events)


@pytest.mark.asyncio
async def test_live_codex_read_only_session(tmp_path: Path) -> None:
    settings = Settings(_env_file=None)
    if shutil.which(settings.codex_cli_path) is None:
        pytest.skip(f"Codex CLI not found: {settings.codex_cli_path}")
    git = await asyncio.create_subprocess_exec(
        "git",
        "init",
        "--quiet",
        "--initial-branch=main",
        str(tmp_path),
    )
    assert await git.wait() == 0
    adapter = CodexCliAdapter(executable=settings.codex_cli_path)

    session = await adapter.start(request_for(AgentRole.IMPLEMENTER, tmp_path))
    events = [event async for event in adapter.stream(session.session_id)]
    result = await adapter.wait(session.session_id)

    assert result.reason is AgentExitReason.COMPLETED
    assert session.native_session_id
    assert any(event.text == "CODECREW_OK" for event in events)
