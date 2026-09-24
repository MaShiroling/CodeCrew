"""Local Claude Code CLI compatibility check; never starts a model turn."""

import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from app.agents import AgentRequest, AgentRole, DeepSeekClaudeReviewerAdapter


def test_installed_claude_cli_exposes_reviewer_flags() -> None:
    executable = shutil.which("claude")
    if executable is None:
        pytest.skip("Claude Code CLI is not installed")

    help_result = subprocess.run(
        [executable, "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert help_result.returncode == 0, "Claude Code --help failed"

    request = AgentRequest(
        task_id=uuid4(),
        trace_id=uuid4(),
        role=AgentRole.REVIEWER,
        prompt="Local flag compatibility check only.",
        working_directory=Path.cwd(),
    )
    command = DeepSeekClaudeReviewerAdapter(executable=executable).build_command(request)
    option_names = {part.split("=", 1)[0] for part in command if part.startswith("--")}
    for option in sorted(option_names):
        assert option in help_result.stdout, f"installed Claude Code lacks {option}"
