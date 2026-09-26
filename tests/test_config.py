import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_load_prefixed_environment(monkeypatch) -> None:
    monkeypatch.setenv("CODECREW_MAX_REWORK_ROUNDS", "1")
    monkeypatch.setenv("CODECREW_AGENT_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("CODECREW_CLAUDE_CLI_PATH", "/opt/bin/claude")
    monkeypatch.setenv("CODECREW_CODEX_CLI_PATH", "/opt/bin/codex")
    monkeypatch.setenv("CODECREW_MAX_CONVERSATION_AGENT_TURNS", "12")
    monkeypatch.setenv("CODECREW_MAX_CONVERSATION_TOKENS", "12345")

    settings = Settings(_env_file=None)

    assert settings.max_rework_rounds == 1
    assert settings.agent_timeout_seconds == 30
    assert settings.claude_cli_path == "/opt/bin/claude"
    assert settings.codex_cli_path == "/opt/bin/codex"
    assert settings.max_conversation_agent_turns == 12
    assert settings.max_conversation_tokens == 12345


def test_agent_process_defaults_are_bounded() -> None:
    settings = Settings(_env_file=None)

    assert settings.agent_output_queue_maxsize == 256
    assert settings.process_terminate_grace_seconds == 2.0


def test_planner_timeout_is_explicit_and_separate(monkeypatch):
    monkeypatch.delenv("CODECREW_PLANNER_TIMEOUT_SECONDS", raising=False)
    assert Settings(_env_file=None).planner_timeout_seconds is None
    monkeypatch.setenv("CODECREW_PLANNER_TIMEOUT_SECONDS", "360")
    settings = Settings(_env_file=None)
    assert settings.planner_timeout_seconds == 360
    assert settings.agent_timeout_seconds == 900


@pytest.mark.parametrize("value", ["0", "-1", "901", "NaN", "Infinity", "", "bad"])
def test_invalid_planner_deadline_fails_configuration(monkeypatch, value):
    monkeypatch.setenv("CODECREW_PLANNER_TIMEOUT_SECONDS", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
