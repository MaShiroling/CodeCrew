"""Local single-worker HTTP entry point with explicit verification policy."""

import argparse
import os
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, ValidationError

from app.agents import (
    AgentRegistry,
    AgentRole,
    ClaudeCodeAdapter,
    CodexCliAdapter,
    DeepSeekClaudeReviewerAdapter,
    PermissionMode,
)
from app.api.runtime import build_task_runtime
from app.config import Settings, get_settings
from app.main import create_app
from app.team import MemberRole
from app.verification import VerificationPlan
from app.workspace import CommandPolicy, PermissionPolicy


class ServerConfig(BaseModel):
    """No credentials: provider authentication remains in each CLI's environment."""

    model_config = ConfigDict(extra="forbid")

    planner_adapter: Literal["claude-code", "codex-cli"]
    reviewer_adapter: Literal["claude-code", "deepseek-claude-reviewer"] = "claude-code"
    verification_plan: VerificationPlan
    permission_policy: PermissionPolicy
    command_policy: CommandPolicy


def load_server_config(path: Path) -> ServerConfig:
    try:
        return ServerConfig.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError(f"invalid server configuration {path}: {exc}") from exc


def build_server_app(config: ServerConfig, *, settings: Settings) -> FastAPI:
    if config.reviewer_adapter == "deepseek-claude-reviewer" and not os.environ.get(
        "DEEPSEEK_API_KEY", ""
    ).strip():
        raise ValueError("DEEPSEEK_API_KEY is required for the DeepSeek reviewer")
    registry = AgentRegistry()
    claude_roles = {AgentRole.REVIEWER} if config.reviewer_adapter == "claude-code" else set()
    codex_roles = {AgentRole.IMPLEMENTER}
    if config.planner_adapter == "claude-code":
        claude_roles.add(AgentRole.PLANNER)
    else:
        codex_roles.add(AgentRole.PLANNER)
    if claude_roles:
        registry.register(
            ClaudeCodeAdapter(executable=settings.claude_cli_path),
            roles=claude_roles,
            permission_modes={PermissionMode.READ_ONLY},
        )
    if config.reviewer_adapter == "deepseek-claude-reviewer":
        registry.register(
            DeepSeekClaudeReviewerAdapter(executable=settings.claude_cli_path),
            roles={AgentRole.REVIEWER},
            permission_modes={PermissionMode.READ_ONLY},
        )
    registry.register(
        CodexCliAdapter(executable=settings.codex_cli_path),
        roles=codex_roles,
        permission_modes={PermissionMode.READ_ONLY, PermissionMode.WORKSPACE_WRITE},
    )
    runtime = build_task_runtime(
        settings=settings,
        registry=registry,
        agent_names={
            MemberRole.PLANNER: config.planner_adapter,
            MemberRole.IMPLEMENTER: "codex-cli",
            MemberRole.REVIEWER: config.reviewer_adapter,
        },
        verification_plan=config.verification_plan,
        permission_policy=config.permission_policy,
        command_policy=config.command_policy,
    )
    return create_app(runtime=runtime)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codecrew")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="start a configured local task API")
    serve.add_argument("--config", type=Path, required=True, help="JSON server policy file")
    serve.add_argument("--port", type=int, default=8000, help="local HTTP port")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        config = load_server_config(args.config)
        settings = get_settings()
        for executable in (settings.claude_cli_path, settings.codex_cli_path):
            if shutil.which(executable) is None:
                raise ValueError(f"required Agent CLI is not available: {executable}")
        app = build_server_app(config, settings=settings)
    except ValueError as exc:
        parser.error(str(exc))
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
