from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import cli
from app.config import Settings

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/server-config.python.json"
CHAT_TEAM_EXAMPLE = Path(__file__).resolve().parents[1] / "examples/server-config.chat-team.json"


def test_example_config_builds_runnable_api(tmp_path: Path) -> None:
    config = cli.load_server_config(EXAMPLE)
    app = cli.build_server_app(
        config,
        settings=Settings(
            database_url=f"sqlite:///{tmp_path / 'tasks.sqlite3'}",
            artifact_root=tmp_path / "artifacts",
            worktree_root=tmp_path / "worktrees",
            standalone_chat_workspace_root=tmp_path / "chat-workspaces",
            standalone_chat_runtime_root=tmp_path / "chat-runtime",
        ),
    )
    assert app.state.task_service is not None
    assert "/api/v1/tasks" in app.openapi()["paths"]
    assert app.state.chat_service is not None
    assert "/api/v1/chats" in app.openapi()["paths"]
    with TestClient(app) as client:
        created = client.post("/api/v1/chats", json={
            "title": "无仓库讨论", "idempotency_key": str(uuid4()),
        })
        assert created.status_code == 201
        assert client.get("/api/v1/tasks").json()["items"] == []
        assert not (tmp_path / "worktrees").exists()


def test_chat_team_example_binds_three_requested_adapters(tmp_path: Path, monkeypatch) -> None:
    config = cli.load_server_config(CHAT_TEAM_EXAMPLE)
    assert (config.planner_adapter, config.implementer_adapter, config.reviewer_adapter) == (
        "codex-cli", "kimi-code-cli", "deepseek-claude-reviewer",
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'tasks.sqlite3'}",
        artifact_root=tmp_path / "artifacts",
        worktree_root=tmp_path / "worktrees",
        standalone_chat_workspace_root=tmp_path / "chat-workspaces",
        standalone_chat_runtime_root=tmp_path / "chat-runtime",
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_MODEL_API_KEY", raising=False)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        cli.build_server_app(config, settings=settings)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    with pytest.raises(ValueError, match="KIMI_MODEL_API_KEY"):
        cli.build_server_app(config, settings=settings)
    monkeypatch.setenv("KIMI_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    app = cli.build_server_app(config, settings=settings)
    assert app.state.task_service.agent_names == {
        cli.MemberRole.PLANNER: "codex-cli",
        cli.MemberRole.IMPLEMENTER: "kimi-code-cli",
        cli.MemberRole.REVIEWER: "deepseek-claude-reviewer",
    }


def test_cli_serve_binds_local_single_worker(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'tasks.sqlite3'}",
        artifact_root=tmp_path / "artifacts",
        worktree_root=tmp_path / "worktrees",
        standalone_chat_workspace_root=tmp_path / "chat-workspaces",
        standalone_chat_runtime_root=tmp_path / "chat-runtime",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/fake/agent")
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **options: calls.append((app, options)))

    assert cli.main(["serve", "--config", str(EXAMPLE), "--port", "8765"]) == 0
    assert len(calls) == 1
    assert calls[0][1] == {"host": "127.0.0.1", "port": 8765, "workers": 1}
    assert calls[0][0].state.task_service is not None


def test_chat_serve_needs_no_task_config_or_agent_cli(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'chat.sqlite3'}",
        standalone_chat_workspace_root=tmp_path / "chat-workspaces",
        standalone_chat_runtime_root=tmp_path / "chat-runtime",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    monkeypatch.delenv("KIMI_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **options: calls.append((app, options)))

    assert cli.main(["chat-serve", "--port", "8766"]) == 0
    assert calls[0][1] == {"host": "127.0.0.1", "port": 8766, "workers": 1}
    with TestClient(calls[0][0]) as client:
        assert client.post("/api/v1/chats", json={
            "title": "纯聊天", "idempotency_key": str(uuid4()),
        }).status_code == 201
        assert client.get("/api/v1/tasks").status_code == 503
    assert not (tmp_path / "worktrees").exists()


def test_cli_rejects_missing_config_and_unavailable_agent(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(ValueError, match="invalid server configuration"):
        cli.load_server_config(tmp_path / "missing.json")

    monkeypatch.setattr(cli, "get_settings", Settings)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--config", str(EXAMPLE)])
    assert exc.value.code == 2


def test_explicit_deepseek_reviewer_binding_requires_key(tmp_path: Path, monkeypatch) -> None:
    config = cli.load_server_config(EXAMPLE).model_copy(
        update={"reviewer_adapter": "deepseek-claude-reviewer"}
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'tasks.sqlite3'}",
        artifact_root=tmp_path / "artifacts",
        worktree_root=tmp_path / "worktrees",
        standalone_chat_workspace_root=tmp_path / "chat-workspaces",
        standalone_chat_runtime_root=tmp_path / "chat-runtime",
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        cli.build_server_app(config, settings=settings)

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    app = cli.build_server_app(config, settings=settings)
    assert app.state.task_service.agent_names[cli.MemberRole.REVIEWER] == (
        "deepseek-claude-reviewer"
    )


def test_explicit_kimi_implementer_binding_requires_key_and_sandbox(
    tmp_path: Path, monkeypatch
) -> None:
    config = cli.load_server_config(EXAMPLE).model_copy(
        update={"implementer_adapter": "kimi-code-cli"}
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'tasks.sqlite3'}",
        artifact_root=tmp_path / "artifacts",
        worktree_root=tmp_path / "worktrees",
        standalone_chat_workspace_root=tmp_path / "chat-workspaces",
        standalone_chat_runtime_root=tmp_path / "chat-runtime",
    )
    monkeypatch.delenv("KIMI_MODEL_API_KEY", raising=False)
    with pytest.raises(ValueError, match="KIMI_MODEL_API_KEY"):
        cli.build_server_app(config, settings=settings)

    monkeypatch.setenv("KIMI_MODEL_API_KEY", "test-key")
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    with pytest.raises(ValueError, match="macOS sandbox-exec"):
        cli.build_server_app(config, settings=settings)

    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    app = cli.build_server_app(config, settings=settings)
    assert app.state.task_service.agent_names[cli.MemberRole.IMPLEMENTER] == "kimi-code-cli"
