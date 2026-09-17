from app.config import Settings


def test_settings_load_prefixed_environment(monkeypatch) -> None:
    monkeypatch.setenv("CODECREW_MAX_REWORK_ROUNDS", "1")
    monkeypatch.setenv("CODECREW_AGENT_TIMEOUT_SECONDS", "30")

    settings = Settings(_env_file=None)

    assert settings.max_rework_rounds == 1
    assert settings.agent_timeout_seconds == 30

