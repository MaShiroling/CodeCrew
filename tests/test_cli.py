from pathlib import Path

import pytest

from app import cli
from app.config import Settings

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/server-config.python.json"


def test_example_config_builds_runnable_api(tmp_path: Path) -> None:
    config = cli.load_server_config(EXAMPLE)
    app = cli.build_server_app(
        config,
        settings=Settings(
            database_url=f"sqlite:///{tmp_path / 'tasks.sqlite3'}",
            artifact_root=tmp_path / "artifacts",
            worktree_root=tmp_path / "worktrees",
        ),
    )
    assert app.state.task_service is not None
    assert "/api/v1/tasks" in app.openapi()["paths"]


def test_cli_serve_binds_local_single_worker(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'tasks.sqlite3'}",
        artifact_root=tmp_path / "artifacts",
        worktree_root=tmp_path / "worktrees",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/fake/agent")
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **options: calls.append((app, options)))

    assert cli.main(["serve", "--config", str(EXAMPLE), "--port", "8765"]) == 0
    assert len(calls) == 1
    assert calls[0][1] == {"host": "127.0.0.1", "port": 8765, "workers": 1}
    assert calls[0][0].state.task_service is not None


def test_cli_rejects_missing_config_and_unavailable_agent(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(ValueError, match="invalid server configuration"):
        cli.load_server_config(tmp_path / "missing.json")

    monkeypatch.setattr(cli, "get_settings", Settings)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--config", str(EXAMPLE)])
    assert exc.value.code == 2
