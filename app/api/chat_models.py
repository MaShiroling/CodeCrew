"""HTTP contracts for independent, non-executing team chat."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.chat.models import StandaloneChatRoom, StoredStandaloneChatMessage
from app.team.models import MAX_CHAT_CONTENT_CHARS


class CreateChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: UUID
    title: str = Field(min_length=1, max_length=200)


class PostChatMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: UUID
    content: str = Field(min_length=1, max_length=MAX_CHAT_CONTENT_CHARS)
    reply_to: UUID | None = None


class ChatRoomPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[StandaloneChatRoom, ...]
    limit: int
    offset: int


class ChatMessagePage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[StoredStandaloneChatMessage, ...]
    after_sequence: int
    limit: int


class ChatMessageReceipt(BaseModel):
    """Persistence confirmation only; no Agent wakeup or coding authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: Literal["standalone_chat"] = "standalone_chat"
    message: StoredStandaloneChatMessage
    execution_authorized: Literal[False] = False
    agent_dispatched: Literal[False] = False
