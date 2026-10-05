"""Local single-worker HTTP entry point with explicit verification policy."""

import argparse
import os
import platform
import shutil
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
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
    FakeAgentAdapter,
    FakeAgentScenario,
    KimiCodeAdapter,
    PermissionMode,
)
from app.api.runtime import build_task_runtime
from app.chat.agents import (
    StandaloneChatAgentRuntime,
    StandaloneChatWorkspaceManager,
    build_standalone_chat_agent_runtime,
)
from app.chat.bounded_dispatch import BoundedDiscussionDispatcher
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


def _build_fake_chat_runtime(settings: Settings) -> StandaloneChatAgentRuntime:
    workspaces = StandaloneChatWorkspaceManager(
        settings.standalone_chat_workspace_root, settings.standalone_chat_runtime_root,
    )
    return StandaloneChatAgentRuntime(workspaces, {
            MemberRole.PLANNER: FakeAgentAdapter(FakeAgentScenario(output={
                "message": '{"content":"白金：先确认目标和验收边界，我请月见补充实现视角。","handoff_to":["implementer"]}',
            })),
            MemberRole.IMPLEMENTER: FakeAgentAdapter(FakeAgentScenario(output={
                "message": '{"content":"月见：实现时要检查兼容性和边界输入，再请鲸鲸审视风险。","handoff_to":["reviewer"]}',
            })),
            MemberRole.REVIEWER: FakeAgentAdapter(FakeAgentScenario(output={
                "message": '{"content":"鲸鲸：需要可复查的测试证据；讨论本身不能证明代码已改好。","handoff_to":[]}',
            })),
    })


def _build_fake_bounded_runtime(settings: Settings) -> StandaloneChatAgentRuntime:
    workspaces = StandaloneChatWorkspaceManager(
        settings.standalone_chat_workspace_root, settings.standalone_chat_runtime_root,
    )
    replies = {
        MemberRole.PLANNER: ("白金：先把讨论边界理清，请月见补充。", "handoff", ["implementer"]),
        MemberRole.IMPLEMENTER: ("月见：我补充具体条件，请鲸鲸检查风险。", "handoff", ["reviewer"]),
        MemberRole.REVIEWER: ("鲸鲸：这些是讨论结论，不是代码或测试证据。", "finish", []),
    }
    return StandaloneChatAgentRuntime(workspaces, {
        role: FakeAgentAdapter(FakeAgentScenario(output={
            "structured_output": {
                "content": content, "next_action": action, "handoff_to": targets,
            },
        }))
        for role, (content, action, targets) in replies.items()
    })


def build_chat_app(*, settings: Settings, fake_agents: bool = False,
                   feishu: bool = False) -> FastAPI:
    """Chat-only app; the explicit demo mode never starts real model CLIs."""
    if feishu:
        if fake_agents:
            raise ValueError("chat-demo cannot start Feishu")
        settings.require_feishu()
        from app.feishu.sender import require_sdk
        require_sdk()
    service = _build_chat_service(settings)
    runtime = (_build_fake_chat_runtime(settings) if fake_agents
               else build_standalone_chat_agent_runtime(settings))
    dispatcher = StandaloneChatDispatcher(
        service.store, runtime,
        timeout_seconds=min(settings.agent_timeout_seconds, 180),
    )
    bounded = BoundedDiscussionDispatcher(
        service.store,
        _build_fake_bounded_runtime(settings) if fake_agents else runtime,
        timeout_seconds=min(settings.agent_timeout_seconds, 180),
    )
    feishu_runtime = None
    if feishu:
        from app.feishu.runtime import build_feishu_runtime
        feishu_runtime = build_feishu_runtime(service, bounded, settings)
    return create_app(chat_service=service, chat_dispatcher=dispatcher,
                      bounded_dispatcher=bounded, feishu_runtime=feishu_runtime)


def build_server_app(
    config: ServerConfig, *, settings: Settings,
    repository_bound: Path | None = None, issue_bound: str | None = None,
    disable_direct_task_creation: bool = False,
    reviewer_home: Path | None = None,
) -> FastAPI:
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
        if reviewer_home is not None:
            reviewer_home.mkdir(mode=0o700, parents=True, exist_ok=False)
        registry.register(
            DeepSeekClaudeReviewerAdapter(
                executable=settings.claude_cli_path,
                env_source=({**os.environ, "HOME": str(reviewer_home)}
                            if reviewer_home is not None else None),
            ),
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
    chat_runtime = build_standalone_chat_agent_runtime(settings)
    chat_dispatcher = StandaloneChatDispatcher(
        chat_service.store, chat_runtime,
        timeout_seconds=min(settings.agent_timeout_seconds, 180),
    )
    bounded_dispatcher = BoundedDiscussionDispatcher(
        chat_service.store, chat_runtime,
        timeout_seconds=min(settings.agent_timeout_seconds, 180),
    )
    return create_app(runtime=runtime, chat_service=chat_service,
                      chat_dispatcher=chat_dispatcher,
                      bounded_dispatcher=bounded_dispatcher,
                      chat_coding_policy=config.permission_policy,
                      chat_coding_repository_bound=repository_bound,
                      chat_coding_issue_bound=issue_bound,
                      disable_direct_task_creation=disable_direct_task_creation)


def _require_clis(config: ServerConfig, settings: Settings) -> None:
    required_executables = {
        settings.claude_cli_path
        if config.planner_adapter == "claude-code" or config.reviewer_adapter in {
            "claude-code", "deepseek-claude-reviewer"
        } else None,
        settings.codex_cli_path
        if config.planner_adapter == "codex-cli" or config.implementer_adapter == "codex-cli"
        else None,
        settings.kimi_cli_path if config.implementer_adapter == "kimi-code-cli" else None,
    }
    for executable in required_executables - {None}:
        if shutil.which(executable) is None:
            raise ValueError(f"required Agent CLI is not available: {executable}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codecrew")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="start a configured local task API")
    serve.add_argument("--config", type=Path, required=True, help="JSON server policy file")
    serve.add_argument("--port", type=int, default=8000, help="local HTTP port")
    chat_serve = commands.add_parser("chat-serve", help="start local read-only team chat")
    chat_serve.add_argument("--port", type=int, default=8000, help="local HTTP port")
    chat_serve.add_argument("--feishu", action="store_true", help="explicitly enable Feishu read-only bridge")
    chat_demo = commands.add_parser("chat-demo", help="start chat UI with three fake Agents")
    chat_demo.add_argument("--port", type=int, default=8000, help="local HTTP port")
    full_demo = commands.add_parser(
        "demo-serve", help="start disposable Fake chat-to-code walkthrough (no model keys)"
    )
    full_demo.add_argument("--port", type=int, default=8000, help="local HTTP port")
    live_demo = commands.add_parser(
        "live-demo", help="start disposable real three-Agent chat-to-code acceptance"
    )
    live_demo.add_argument("--port", type=int, default=8000, help="local HTTP port")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if args.command == "demo-serve":
        from app.demo import build_demo_app

        with TemporaryDirectory(prefix="codecrew-demo-") as directory:
            app, repository = build_demo_app(Path(directory).resolve())
            print(f"Fake 演示仓库：{repository}", flush=True)
            print("只支持演示任务：只修改 src/app.py，把 value 从 1 改为 2。", flush=True)
            print(f"打开 http://127.0.0.1:{args.port}/ui/chat/", flush=True)
            print("停止服务后临时演示仓库、Worktree 与证据将被删除。", flush=True)
            uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1)
        return 0
    if args.command == "live-demo":
        from scripts.chat_to_code_live_fixture import ISSUE, build_config, create_repository

        try:
            config = build_config()
            base_settings = Settings(_env_file=None)
            _require_clis(config, base_settings)
            with TemporaryDirectory(prefix="codecrew-live-demo-") as directory:
                root = Path(directory).resolve()
                repository = create_repository(root)
                settings = base_settings.model_copy(update={
                    "database_url": f"sqlite:///{root / 'task.sqlite3'}",
                    "artifact_root": root / "artifacts",
                    "worktree_root": root / "worktrees",
                    "standalone_chat_workspace_root": root / "chat-workspaces",
                    "standalone_chat_runtime_root": root / "chat-runtime",
                    "planner_timeout_seconds": base_settings.planner_timeout_seconds or 360,
                })
                app = build_server_app(
                    config, settings=settings, repository_bound=repository,
                    issue_bound=ISSUE, disable_direct_task_creation=True,
                    reviewer_home=root / "reviewer-home",
                )
                print(f"真实模型临时演示仓库：{repository}", flush=True)
                print("目标已在授权面板预填；仅允许这个仓库和固定小修复。", flush=True)
                print(f"打开 http://127.0.0.1:{args.port}/ui/chat/", flush=True)
                print("停止服务后临时仓库、Worktree 和证据将被删除。", flush=True)
                uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1)
            return 0
        except ValueError as exc:
            parser.error(str(exc))
    try:
        settings = get_settings()
        if args.command in {"chat-serve", "chat-demo"}:
            options = {"feishu": True} if args.command == "chat-serve" and args.feishu else {}
            app = build_chat_app(settings=settings, fake_agents=args.command == "chat-demo", **options)
        else:
            config = load_server_config(args.config)
            _require_clis(config, settings)
            app = build_server_app(config, settings=settings)
    except ValueError as exc:
        parser.error(str(exc))
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
