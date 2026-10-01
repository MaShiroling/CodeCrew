"""HTTP contracts for independent, read-only team chat."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.chat.models import StandaloneChatRoom, StandaloneChatTurn, StoredStandaloneChatMessage
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
    context_anchor_id: UUID | None = None


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
    """Persistence and queue confirmation; never grants coding authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: Literal["standalone_chat"] = "standalone_chat"
    message: StoredStandaloneChatMessage
    execution_authorized: Literal[False] = False
    # Legacy synchronous-dispatch indicator. Use `turns` for async Agent status.
    agent_dispatched: Literal[False] = False
    discussion_queued: bool = False
    turns: tuple[StandaloneChatTurn, ...] = ()


class ChatTurnPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[StandaloneChatTurn, ...]
