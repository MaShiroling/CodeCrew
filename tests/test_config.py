from app.config import Settings


def test_settings_load_prefixed_environment(monkeypatch) -> None:
    monkeypatch.setenv("CODECREW_MAX_REWORK_ROUNDS", "1")
    monkeypatch.setenv("CODECREW_AGENT_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("CODECREW_CLAUDE_CLI_PATH", "/opt/bin/claude")
    monkeypatch.setenv("CODECREW_CODEX_CLI_PATH", "/opt/bin/codex")

    settings = Settings(_env_file=None)

    assert settings.max_rework_rounds == 1
    assert settings.agent_timeout_seconds == 30
    assert settings.claude_cli_path == "/opt/bin/claude"
    assert settings.codex_cli_path == "/opt/bin/codex"


def test_agent_process_defaults_are_bounded() -> None:
    settings = Settings(_env_file=None)

    assert settings.agent_output_queue_maxsize == 256
    assert settings.process_terminate_grace_seconds == 2.0
