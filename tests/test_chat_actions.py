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
    with pytest.raises(ValidationError, match="exactly one"):
        AgentChatAction(
            action=ChatActionType.SHARE_PLAN,
            recipient=recipient(),
            content="plan",
        )
    inline = AgentChatAction(
        action=ChatActionType.SHARE_PLAN,
        recipient=recipient(),
        content="plan",
        artifact_content={"steps": ["edit", "test"]},
    )
    assert inline.artifact_content == {"steps": ["edit", "test"]}


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


def test_plan_revision_fields_are_scoped_to_share_plan() -> None:
    question_id = uuid4()
    previous_plan_id = uuid4()
    revision = AgentChatAction(
        action=ChatActionType.SHARE_PLAN,
        recipient=recipient(),
        content="revised plan",
        artifact_content={"steps": ["clarified change"]},
        supersedes_artifact_id=previous_plan_id,
        addresses_message_ids=(question_id,),
    )

    assert revision.supersedes_artifact_id == previous_plan_id
    assert revision.addresses_message_ids == (question_id,)
    with pytest.raises(ValidationError, match="only allowed for share_plan"):
        AgentChatAction(
            action=ChatActionType.SEND_MESSAGE,
            recipient=recipient(),
            content="invalid",
            addresses_message_ids=(question_id,),
        )


def test_rework_action_requires_structured_review_evidence() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        AgentChatAction(
            action=ChatActionType.REQUEST_REWORK,
            recipient=recipient(),
            content="fix the regression",
        )
    action = AgentChatAction(
        action=ChatActionType.REQUEST_REWORK,
        recipient=recipient(),
        content="fix the regression",
        artifact_content={
            "issues": [
                {"priority": "high", "summary": "fallback regresses", "resolved": False}
            ]
        },
    )
    assert action.artifact_content is not None
