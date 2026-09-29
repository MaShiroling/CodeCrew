"""Local single-worker HTTP entry point with explicit verification policy."""

import argparse
import os
import platform
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
    KimiCodeAdapter,
    PermissionMode,
)
from app.api.runtime import build_task_runtime
from app.chat.agents import build_standalone_chat_agent_runtime
from app.chat.dispatch import StandaloneChatDispatcher
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.config import Settings, get_settings
from app.main import create_app
from app.storage import SQLiteDatabase
from app.team import MemberRole
from app.verification import VerificationPlan
from app.workspace import CommandPolicy, PermissionPolicy


class ServerConfig(BaseModel):
    """No credentials: provider authentication remains in each CLI's environment."""

    model_config = ConfigDict(extra="forbid")

    planner_adapter: Literal["claude-code", "codex-cli"]
    implementer_adapter: Literal["codex-cli", "kimi-code-cli"] = "codex-cli"
    reviewer_adapter: Literal["claude-code", "deepseek-claude-reviewer"] = "claude-code"
    verification_plan: VerificationPlan
    permission_policy: PermissionPolicy
    command_policy: CommandPolicy


def load_server_config(path: Path) -> ServerConfig:
    try:
        return ServerConfig.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError(f"invalid server configuration {path}: {exc}") from exc


def _build_chat_service(settings: Settings) -> StandaloneChatService:
    if not settings.database_url.startswith("sqlite:///"):
        raise ValueError("only sqlite:/// database URLs are supported")
    database_path = settings.database_url.removeprefix("sqlite:///")
    if not database_path:
        raise ValueError("SQLite database path must not be empty")
    chat_store = StandaloneChatStore(SQLiteDatabase(Path(database_path)))
    chat_store.initialize()
    return StandaloneChatService(chat_store)


def build_chat_app(*, settings: Settings) -> FastAPI:
    """Chat-only app: no task runtime or startup provider credential checks."""
    service = _build_chat_service(settings)
    dispatcher = StandaloneChatDispatcher(
        service.store, build_standalone_chat_agent_runtime(settings),
        timeout_seconds=min(settings.agent_timeout_seconds, 180),
    )
    return create_app(chat_service=service, chat_dispatcher=dispatcher)


def build_server_app(config: ServerConfig, *, settings: Settings) -> FastAPI:
    if config.reviewer_adapter == "deepseek-claude-reviewer" and not os.environ.get(
        "DEEPSEEK_API_KEY", ""
    ).strip():
        raise ValueError("DEEPSEEK_API_KEY is required for the DeepSeek reviewer")
    if config.implementer_adapter == "kimi-code-cli":
        if not os.environ.get("KIMI_MODEL_API_KEY", "").strip():
            raise ValueError(
                "KIMI_MODEL_API_KEY (Kimi Code membership key) is required for isolated Kimi CLI"
            )
        if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").is_file():
            raise ValueError("Kimi Code implementer requires macOS sandbox-exec")
    registry = AgentRegistry()
    claude_roles = {AgentRole.REVIEWER} if config.reviewer_adapter == "claude-code" else set()
    codex_roles = {AgentRole.IMPLEMENTER} if config.implementer_adapter == "codex-cli" else set()
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
    if codex_roles:
        registry.register(
            CodexCliAdapter(executable=settings.codex_cli_path),
            roles=codex_roles,
            permission_modes=(
                {PermissionMode.READ_ONLY, PermissionMode.WORKSPACE_WRITE}
                if AgentRole.IMPLEMENTER in codex_roles else {PermissionMode.READ_ONLY}
            ),
        )
    if config.implementer_adapter == "kimi-code-cli":
        registry.register(
            KimiCodeAdapter(
                worktree_root=settings.worktree_root,
                runtime_root=settings.worktree_root.parent / "kimi-runtime",
                policy=config.permission_policy,
                executable=settings.kimi_cli_path,
            ),
            roles={AgentRole.IMPLEMENTER},
            permission_modes={PermissionMode.WORKSPACE_WRITE, PermissionMode.READ_ONLY},
        )
    runtime = build_task_runtime(
        settings=settings,
        registry=registry,
        agent_names={
            MemberRole.PLANNER: config.planner_adapter,
            MemberRole.IMPLEMENTER: config.implementer_adapter,
            MemberRole.REVIEWER: config.reviewer_adapter,
        },
        verification_plan=config.verification_plan,
        permission_policy=config.permission_policy,
        command_policy=config.command_policy,
    )
    chat_service = _build_chat_service(settings)
    chat_dispatcher = StandaloneChatDispatcher(
        chat_service.store, build_standalone_chat_agent_runtime(settings),
        timeout_seconds=min(settings.agent_timeout_seconds, 180),
    )
    return create_app(runtime=runtime, chat_service=chat_service,
                      chat_dispatcher=chat_dispatcher)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codecrew")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="start a configured local task API")
    serve.add_argument("--config", type=Path, required=True, help="JSON server policy file")
    serve.add_argument("--port", type=int, default=8000, help="local HTTP port")
    chat_serve = commands.add_parser("chat-serve", help="start local read-only team chat")
    chat_serve.add_argument("--port", type=int, default=8000, help="local HTTP port")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        settings = get_settings()
        if args.command == "chat-serve":
            app = build_chat_app(settings=settings)
        else:
            config = load_server_config(args.config)
            required_executables = {
                settings.claude_cli_path
                if config.planner_adapter == "claude-code" or config.reviewer_adapter in {"claude-code", "deepseek-claude-reviewer"}
                else None,
                settings.codex_cli_path
                if config.planner_adapter == "codex-cli" or config.implementer_adapter == "codex-cli"
                else None,
                settings.kimi_cli_path if config.implementer_adapter == "kimi-code-cli" else None,
            }
            for executable in required_executables - {None}:
                if shutil.which(executable) is None:
                    raise ValueError(f"required Agent CLI is not available: {executable}")
            app = build_server_app(config, settings=settings)
    except ValueError as exc:
        parser.error(str(exc))
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
