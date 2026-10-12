from functools import lru_cache
from pathlib import Path
from typing import Literal

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
    memory_lexical_backend: Literal["legacy", "bm25"] = "legacy"
    # Qdrant recall is opt-in; remote initialization/subscription/delivery stay explicit.
    memory_vector_store: Literal["sqlite", "qdrant"] = "sqlite"
    memory_qdrant_url: str = "http://127.0.0.1:6333"
    memory_qdrant_collection_prefix: str = "codecrew_memory"
    memory_qdrant_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    memory_qdrant_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    # Explicit --watch also required; no chat startup hook or automatic target setup.
    memory_vector_worker_enabled: bool = False
    memory_vector_worker_batch_size: int = Field(default=32, ge=1, le=200)
    memory_vector_worker_poll_interval_seconds: float = Field(default=2.0, ge=0.1, le=60, allow_inf_nan=False)
    memory_vector_worker_shutdown_grace_seconds: float = Field(default=10.0, ge=0.1, le=60, allow_inf_nan=False)
    memory_embedding_mode: Literal["off", "on"] = "off"
    memory_embedding_base_url: str = ""
    memory_embedding_model: str = ""
    memory_embedding_revision: str = ""
    memory_embedding_api_key: SecretStr = Field(
        default_factory=lambda: SecretStr(""), repr=False,
    )
    memory_embedding_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    memory_embedding_min_similarity: float = Field(default=0.65, ge=-1, le=1)
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

    @field_validator("memory_vector_worker_batch_size", "memory_vector_worker_poll_interval_seconds",
                     "memory_vector_worker_shutdown_grace_seconds", mode="before")
    @classmethod
    def reject_vector_worker_numeric_bools(cls, value):
        if isinstance(value, bool):
            # Pydantic v2 wraps ValueError, not TypeError, as a settings validation error.
            raise ValueError("worker numeric settings must not be bool")  # noqa: TRY004
        return value

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
