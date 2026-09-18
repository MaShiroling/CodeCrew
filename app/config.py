from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from CODECREW_* environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="CODECREW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    database_url: str = "sqlite:///./codecrew.db"
    artifact_root: Path = Path(".codecrew/artifacts")
    worktree_root: Path = Path(".codecrew/worktrees")
    max_rework_rounds: int = Field(default=2, ge=0, le=10)
    agent_timeout_seconds: int = Field(default=900, gt=0)
    claude_cli_path: str = Field(default="claude", min_length=1)
    codex_cli_path: str = Field(default="codex", min_length=1)
    agent_output_queue_maxsize: int = Field(default=256, gt=0)
    process_terminate_grace_seconds: float = Field(default=2.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
