import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.team import (
    AgentChatAction,
    AgentChatTurn,
    ChatActionError,
    ChatActionType,
    MemberRole,
    MessageRecipient,
    RecipientKind,
    parse_agent_chat_turn,
)


def recipient() -> MessageRecipient:
    return MessageRecipient(kind=RecipientKind.ROLE, role=MemberRole.PLANNER)


def test_parses_structured_and_json_text_turns() -> None:
    payload = {
        "actions": [
            {
                "action": "ask_question",
                "recipient": {"kind": "role", "role": "planner"},
                "content": "What is the expected fallback?",
            },
            {"action": "finish_turn", "content": "Waiting for clarification"},
        ]
    }

    direct = parse_agent_chat_turn(payload)
    encoded = parse_agent_chat_turn({"message": json.dumps(payload)})

    assert direct == encoded
    assert direct.actions[0].action is ChatActionType.ASK_QUESTION


def test_turn_requires_one_final_finish_action() -> None:
    message = AgentChatAction(
        action=ChatActionType.SEND_MESSAGE,
        recipient=recipient(),
        content="hello",
    )
    finish = AgentChatAction(action=ChatActionType.FINISH_TURN, content="done")

    with pytest.raises(ValidationError, match="must end"):
        AgentChatTurn(actions=(message,))
    with pytest.raises(ValidationError, match="must end"):
        AgentChatTurn(actions=(finish, message))


def test_action_specific_fields_are_required() -> None:
    with pytest.raises(ValidationError, match="requires reply_to"):
        AgentChatAction(
            action=ChatActionType.ANSWER_QUESTION,
            recipient=recipient(),
            content="answer",
        )
    with pytest.raises(ValidationError, match="requires artifact_ids"):
        AgentChatAction(
            action=ChatActionType.SHARE_ARTIFACT,
            recipient=recipient(),
            content="evidence",
        )
    with pytest.raises(ValidationError, match="cannot target"):
        AgentChatAction(
            action=ChatActionType.FINISH_TURN,
            recipient=recipient(),
            content="done",
        )


def test_parser_rejects_non_json_and_unknown_fields() -> None:
    with pytest.raises(ChatActionError, match="not valid JSON"):
        parse_agent_chat_turn({"result": "not-json"})
    with pytest.raises(ChatActionError, match="invalid agent chat turn"):
        parse_agent_chat_turn(
            {
                "actions": [
                    {"action": "finish_turn", "content": "done", "unknown": uuid4()}
                ]
            }
        )
