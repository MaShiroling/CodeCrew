from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agents.timeouts import MAX_PLANNER_TIMEOUT_SECONDS


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
    standalone_chat_workspace_root: Path = Field(
        default_factory=lambda: Path.home() / ".codecrew/chat-workspaces"
    )
    standalone_chat_runtime_root: Path = Field(
        default_factory=lambda: Path.home() / ".codecrew/chat-runtime"
    )
    max_rework_rounds: int = Field(default=2, ge=0, le=10)
    agent_timeout_seconds: int = Field(default=900, gt=0)
    reviewer_structured_output: bool = False
    planner_timeout_seconds: int | None = Field(
        default=None, ge=1, le=MAX_PLANNER_TIMEOUT_SECONDS
    )
    claude_cli_path: str = Field(default="claude", min_length=1)
    codex_cli_path: str = Field(default="codex", min_length=1)
    kimi_cli_path: str = Field(default="kimi", min_length=1)
    agent_output_queue_maxsize: int = Field(default=256, gt=0)
    process_terminate_grace_seconds: float = Field(default=2.0, gt=0)
    max_conversation_agent_turns: int = Field(default=30, ge=0, le=1000)
    max_conversation_tokens: int = Field(default=500_000, ge=0)
    max_conversation_duration_seconds: int = Field(default=7_200, ge=0)
    max_conversation_messages: int = Field(default=200, ge=1, le=1000)
    max_repeated_messages: int = Field(default=3, ge=1, le=20)
    max_questions_without_progress: int = Field(default=4, ge=1, le=20)
    feishu_enabled: bool = False
    feishu_app_id: str = Field(default="", repr=False)
    feishu_app_secret: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    feishu_bot_open_id: str = Field(default="", repr=False)
    feishu_allowed_chat_ids: frozenset[str] = Field(default_factory=frozenset, repr=False)
    feishu_allowed_sender_open_ids: frozenset[str] = Field(default_factory=frozenset, repr=False)
    feishu_max_outbox_attempts: int = Field(default=5, ge=1, le=10)
    feishu_retry_base_seconds: float = Field(default=2, ge=0.1, le=60)
    feishu_retry_cap_seconds: float = Field(default=60, ge=1, le=600)

    @field_validator("feishu_allowed_chat_ids", "feishu_allowed_sender_open_ids")
    @classmethod
    def validate_feishu_allowlist(cls, values: frozenset[str]) -> frozenset[str]:
        import re
        if any(not re.fullmatch(r"[A-Za-z0-9_.:-]{1,255}", value) for value in values):
            raise ValueError("Feishu allowlist must contain nonempty platform IDs")
        return values

    def require_feishu(self) -> None:
        """Invoked only by explicit --feishu, never by ordinary settings loading."""
        if not self.feishu_enabled:
            raise ValueError("--feishu requires CODECREW_FEISHU_ENABLED=true")
        if not self.feishu_app_id.strip() or not self.feishu_app_secret.get_secret_value().strip():
            raise ValueError("Feishu requires CODECREW_FEISHU_APP_ID and CODECREW_FEISHU_APP_SECRET")
        if not self.feishu_allowed_chat_ids or not self.feishu_allowed_sender_open_ids:
            raise ValueError("Feishu requires nonempty chat and sender allowlists (JSON arrays)")
        if self.feishu_retry_cap_seconds < self.feishu_retry_base_seconds:
            raise ValueError("Feishu retry cap must not be smaller than its base delay")


@lru_cache
def get_settings() -> Settings:
    return Settings()
