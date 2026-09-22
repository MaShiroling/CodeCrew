import json
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.team.models import MessageRecipient

MAX_ACTIONS_PER_TURN = 20


class ChatActionError(RuntimeError):
    """Raised when an Agent returns an invalid structured chat turn."""


class ChatActionType(str, Enum):
    SEND_MESSAGE = "send_message"
    ASK_QUESTION = "ask_question"
    ANSWER_QUESTION = "answer_question"
    SHARE_ARTIFACT = "share_artifact"
    REPORT_PROGRESS = "report_progress"
    REQUEST_REVIEW = "request_review"
    REQUEST_REWORK = "request_rework"
    REQUEST_HUMAN_INPUT = "request_human_input"
    FINISH_TURN = "finish_turn"


class AgentChatAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    action: ChatActionType
    recipient: MessageRecipient | None = None
    content: str | None = Field(default=None, min_length=1, max_length=16_000)
    artifact_ids: tuple[UUID, ...] = Field(default=(), max_length=50)
    reply_to: UUID | None = None

    @model_validator(mode="after")
    def validate_action_shape(self) -> "AgentChatAction":
        if self.action is ChatActionType.FINISH_TURN:
            if self.recipient is not None or self.artifact_ids or self.reply_to is not None:
                raise ValueError("finish_turn cannot target recipients, replies, or artifacts")
            return self
        if self.recipient is None:
            raise ValueError("chat actions require a recipient")
        if self.content is None:
            raise ValueError("chat actions require content")
        if self.action is ChatActionType.ANSWER_QUESTION and self.reply_to is None:
            raise ValueError("answer_question requires reply_to")
        if self.action is ChatActionType.SHARE_ARTIFACT and not self.artifact_ids:
            raise ValueError("share_artifact requires artifact_ids")
        if len(self.artifact_ids) != len(set(self.artifact_ids)):
            raise ValueError("artifact_ids must be unique")
        return self


class AgentChatTurn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    actions: tuple[AgentChatAction, ...] = Field(
        min_length=1, max_length=MAX_ACTIONS_PER_TURN
    )

    @model_validator(mode="after")
    def validate_terminal_action(self) -> "AgentChatTurn":
        finishes = [
            index
            for index, action in enumerate(self.actions)
            if action.action is ChatActionType.FINISH_TURN
        ]
        if finishes != [len(self.actions) - 1]:
            raise ValueError("each turn must end with exactly one finish_turn action")
        return self


def parse_agent_chat_turn(output: dict[str, Any]) -> AgentChatTurn:
    candidate: Any = output.get("turn")
    if candidate is None and "actions" in output:
        candidate = output
    if candidate is None:
        candidate = output.get("result", output.get("message"))
    if isinstance(candidate, str):
        try:
            candidate = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ChatActionError("agent chat output is not valid JSON") from exc
    if not isinstance(candidate, dict):
        raise ChatActionError("agent output does not contain a chat turn object")
    try:
        return AgentChatTurn.model_validate(candidate)
    except ValidationError as exc:
        raise ChatActionError(f"invalid agent chat turn: {exc}") from exc
