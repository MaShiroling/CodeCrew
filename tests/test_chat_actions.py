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


def test_native_contract_uses_only_structured_object():
    turn = {"actions": [{"action": "finish_turn"}]}
    parsed = parse_agent_chat_turn(
        {"structured_output": turn, "result": "malformed { JSON"}, require_structured_output=True,
    )
    assert parsed.actions[-1].action is ChatActionType.FINISH_TURN


@pytest.mark.parametrize("native", [None, "{}", [], {"actions": []}, {"actions": [{"action": "finish_turn"}], "unknown": 1}])
def test_native_contract_never_falls_back_to_valid_text(native):
    with pytest.raises(ChatActionError):
        parse_agent_chat_turn(
            {"structured_output": native, "result": '{"actions":[{"action":"finish_turn"}]}'},
            require_structured_output=True,
        )


def test_chat_syntax_error_reports_actual_location_without_echoing_output():
    text = '{"actions": [{"content": "private"}, "artifact_content": {}]}'
    with pytest.raises(ChatActionError) as error:
        parse_agent_chat_turn({"result": text})
    cause = error.value.__cause__
    assert isinstance(cause, json.JSONDecodeError)
    assert f"offset {cause.pos}" in str(error.value) and cause.pos > 0
    assert "private" not in str(error.value)


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
            {"actions": [{"action": "finish_turn", "content": "done", "unknown": uuid4()}]}
        )


@pytest.mark.parametrize("field", ["turn", "message", "result"])
def test_parser_accepts_one_explicit_json_fence_with_optional_prose(field: str) -> None:
    payload = {"actions": [{"action": "finish_turn", "content": "done"}]}
    raw = json.dumps(payload)
    assert parse_agent_chat_turn({field: f" \n```json\n{raw}\n```\n"}) == (
        parse_agent_chat_turn(payload)
    )
    for accepted in (
        f"Here is the answer:\n```json\n{raw}\n```",
        f"```json\n{raw}\n```\nDone",
        f"已阅读 Plan v1。\n\n{raw}",
    ):
        assert parse_agent_chat_turn({field: accepted}) == parse_agent_chat_turn(payload)
    for invalid in (
        f"```json\n{raw}\n```\n```json\n{raw}\n```",
        f"```\n{raw}\n```",
        f"```python\n{raw}\n```",
    ):
        with pytest.raises(ChatActionError, match="not valid JSON"):
            parse_agent_chat_turn({field: invalid})


def test_fenced_json_still_requires_action_schema() -> None:
    for payload in (
        {"verdict": "approved", "summary": "legacy reviewer response", "issues": []},
        {"actions": [{"action": "finish_turn", "unexpected": True}]},
        {"actions": []},
    ):
        with pytest.raises(ChatActionError, match="invalid agent chat turn"):
            parse_agent_chat_turn(
                {"result": f"I approved it.\n```json\n{json.dumps(payload)}\n```"}
            )
        with pytest.raises(ChatActionError, match="invalid agent chat turn"):
            parse_agent_chat_turn({"result": f"说明\n{json.dumps(payload)}"})


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
            "issues": [{"priority": "high", "summary": "fallback regresses", "resolved": False}]
        },
    )
    assert action.artifact_content is not None


@pytest.mark.parametrize("action", ["approve_review", "request_rework"])
@pytest.mark.parametrize("source", ["inline", "existing", "both", "neither"])
def test_review_report_sources_remain_mutually_exclusive(action: str, source: str) -> None:
    payload = {
        "action": action,
        "recipient": {"kind": "role", "role": "orchestrator"},
        "content": "Review summary based on inspected input evidence",
    }
    if source in {"inline", "both"}:
        payload["artifact_content"] = {"issues": []}
    if source in {"existing", "both"}:
        payload["artifact_ids"] = [str(uuid4())]
    turn = {"actions": [payload, {"action": "finish_turn"}]}
    if source in {"both", "neither"}:
        with pytest.raises(ChatActionError, match="requires exactly one"):
            parse_agent_chat_turn({"result": json.dumps(turn)})
    else:
        parsed = parse_agent_chat_turn(turn)
        assert bool(parsed.actions[0].artifact_ids) == (source == "existing")
        assert (parsed.actions[0].artifact_content is not None) == (source == "inline")
