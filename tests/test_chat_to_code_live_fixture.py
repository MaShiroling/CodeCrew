"""Offline checks for the opt-in real-model P3.5 acceptance fixture."""

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cli
from scripts.chat_to_code_live_fixture import (
    EXPECTED_SOURCE,
    INITIAL_SOURCE,
    ISSUE,
    build_config,
    create_repository,
)


def test_live_fixture_starts_clean_and_really_needs_a_fix(tmp_path: Path) -> None:
    repository = create_repository(tmp_path)
    config = build_config()
    assert (repository / "src/app.py").read_text(encoding="utf-8") == INITIAL_SOURCE
    assert ISSUE
    assert config.permission_policy.allowed_paths == ("src",)
    assert [command.kind.value for command in config.verification_plan.commands] == [
        "static_analysis", "public_tests", "hidden_tests",
    ]
    for command in config.verification_plan.commands:
        assert any(rule.matches(command.argv) for rule in config.command_policy.rules)
    results = {
        command.name: subprocess.run(command.argv, cwd=repository, capture_output=True,
                                     timeout=30, check=False).returncode
        for command in config.verification_plan.commands
    }
    assert results == {"syntax": 0, "public": 1, "held-out": 1}
    (repository / "src/app.py").write_text(EXPECTED_SOURCE, encoding="utf-8")
    assert all(subprocess.run(command.argv, cwd=repository, capture_output=True,
                              timeout=30, check=False).returncode == 0
               for command in config.verification_plan.commands)


def test_live_demo_requires_keys_before_starting_server(monkeypatch) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/fake/cli")
    monkeypatch.delenv("KIMI_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *_args, **_kwargs: pytest.fail(
        "server must not start without credentials"
    ))
    with pytest.raises(SystemExit) as error:
        cli.main(["live-demo", "--port", "8769"])
    assert error.value.code == 2


def test_live_demo_composes_bound_app_without_calling_models(monkeypatch) -> None:
    monkeypatch.setenv("KIMI_MODEL_API_KEY", "offline-test-only")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test-only")
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/fake/cli")
    roots = []

    def fake_run(app, **kwargs):
        service = app.state.chat_coding_service
        repository = service.repository_bound
        roots.append(repository.parent)
        assert service.issue_bound == ISSUE
        assert repository.is_dir()
        assert app.state.disable_direct_task_creation is True
        assert kwargs == {"host": "127.0.0.1", "port": 8769, "workers": 1}
        with TestClient(app) as client:
            assert client.get("/api/v1/chats/coding-capability").json()[
                "demo_repository_path"
            ] == str(repository)
            assert client.post("/api/v1/tasks", json={
                "issue": ISSUE, "repository_path": str(repository),
            }).status_code == 503

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    assert cli.main(["live-demo", "--port", "8769"]) == 0
    assert len(roots) == 1 and not roots[0].exists()
