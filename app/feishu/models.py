"""Validated platform-neutral values at the Feishu SDK boundary."""

import hashlib
import json
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.chat.models import ExternalChatSource
from app.team.models import MAX_CHAT_CONTENT_CHARS

PlatformId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,255}$")]


class FeishuInbound(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True, strict=True)

    app_id: PlatformId = Field(repr=False)
    event_id: PlatformId = Field(repr=False)
    message_id: PlatformId = Field(repr=False)
    chat_id: PlatformId = Field(repr=False)
    chat_type: Literal["p2p", "group"]
    sender_open_id: PlatformId = Field(repr=False)
    text: str = Field(min_length=1, max_length=MAX_CHAT_CONTENT_CHARS, repr=False)
    mentions_bot: bool
    display_name: str | None = Field(default=None, min_length=1, max_length=60, repr=False)

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str | None:
        return ExternalChatSource.safe_display_name(value)

    def fingerprint(self) -> str:
        # Redelivered platform messages may have another event ID or display name.
        content = self.model_dump(exclude={"event_id", "display_name"})
        return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


class ConnectionState(str, Enum):
    DISABLED = "disabled"
    STARTING = "starting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"
    STOPPED = "stopped"


class IngressResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    disposition: str
    room_id: UUID | None = None
    message_id: UUID | None = None
    correlation_id: UUID | None = None
    run_id: UUID | None = None


class FeishuStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    connection_state: ConnectionState = ConnectionState.DISABLED
    binding_count: int = 0
    pending_outbox_count: int = 0
    retry_count: int = 0
    failed_count: int = 0
    last_error: str | None = None
