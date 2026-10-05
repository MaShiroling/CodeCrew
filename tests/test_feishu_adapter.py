import json
from copy import deepcopy

import pytest
from feishu_helpers import inbound
from pydantic import ValidationError

from app.chat.models import ExternalChatSource
from app.chat.service import ChatInvalid, StandaloneChatService
from app.feishu.adapter import FeishuAdapter
from app.feishu.models import FeishuInbound
from app.team.models import MemberRole


def payload(**message):
    return {"header": {"app_id": "cli_test", "event_id": "ev_1", "event_type": "im.message.receive_v1"},
            "event": {"sender": {"sender_type": "user", "sender_id": {"open_id": "ou_alice"}},
                      "message": {"message_id": "om_1", "chat_id": "oc_dm", "chat_type": "p2p",
                                  "message_type": "text", "content": json.dumps({"text": "讨论"}),
                                  "mentions": [], **message}}}


def test_dm_and_real_current_bot_group_mention():
    adapter = FeishuAdapter("cli_test", "ou_bot")
    assert adapter.parse(payload()).inbound == inbound(text="讨论")
    event = payload(chat_type="group", content=json.dumps({"text": "@_user_1 @鲸鲸 审阅"}),
                    mentions=[{"key": "@_user_1", "id": {"open_id": "ou_bot"}}])
    parsed = adapter.parse(event).inbound
    assert parsed.text == "@鲸鲸 审阅"
    assert parsed.mentions_bot is True
    assert StandaloneChatService.external_opening_role(parsed.text) is MemberRole.REVIEWER
    # No BOT_OPEN_ID means fail closed for groups, while DM still works.
    assert FeishuAdapter("cli_test", None).parse(event).inbound is None
    assert FeishuAdapter("cli_test", None).parse(payload()).inbound is not None


@pytest.mark.parametrize(("text", "mentions"), [
    ("@CodeCrew @白金 讨论", []), ("@all 讨论", [{"key": "@all", "id": {"open_id": "all"}}]),
    ("@_user_1 讨论", [{"key": "@_user_1", "id": {"open_id": "ou_other_bot"}}]),
    ("@白金 讨论", [{"key": "@白金", "id": {"open_id": "ou_bot"}}]),
])
def test_group_text_all_and_other_bot_do_not_admit(text, mentions):
    result = FeishuAdapter("cli_test", "ou_bot").parse(payload(
        chat_type="group", content=json.dumps({"text": text}), mentions=mentions))
    assert result.inbound is None


@pytest.mark.parametrize("change", [
    {"message_type": "image"}, {"message_type": "file"}, {"message_type": "post"},
    {"content": "not-json"}, {"content": "[]"}, {"content": '{"text":0}'},
    {"content": '{"text":"  "}'}, {"content": json.dumps({"text": "x" * 16001})},
    {"chat_id": "oc_bad\nsecret"}, {"mentions": "invalid"}, {"chat_type": "unknown"},
])
def test_invalid_and_unsupported_messages_are_dropped(change):
    assert FeishuAdapter("cli_test", "ou_bot").parse(payload(**change)).inbound is None


@pytest.mark.parametrize("change", ["bot", "self", "app", "event", "missing"])
def test_echo_and_wrong_app_event(change):
    event = deepcopy(payload())
    if change == "bot":
        event["event"]["sender"]["sender_type"] = "app"
    elif change == "self":
        event["event"]["sender"]["sender_id"]["open_id"] = "ou_bot"
    elif change == "app":
        event["header"]["app_id"] = "cli_other"
    elif change == "event":
        event["header"]["event_type"] = "other_event"
    else:
        del event["event"]["sender"]
    assert FeishuAdapter("cli_test", "ou_bot").parse(event).inbound is None


@pytest.mark.parametrize(("text", "role"), [
    ("讨论", MemberRole.PLANNER), ("@白金 讨论", MemberRole.PLANNER),
    ("@codex 讨论", MemberRole.PLANNER), ("@月见 讨论", MemberRole.IMPLEMENTER),
    ("@kimi 讨论", MemberRole.IMPLEMENTER), ("@鲸鲸 讨论", MemberRole.REVIEWER),
    ("@deepseek 讨论", MemberRole.REVIEWER),
])
def test_explicit_agent_aliases_and_default(text, role):
    assert StandaloneChatService.external_opening_role(text) is role


@pytest.mark.parametrize("text", ["@unknown 讨论", "@白金", "@all 讨论", "  "])
def test_unknown_or_empty_route_rejected(text):
    with pytest.raises(ChatInvalid):
        StandaloneChatService.external_opening_role(text)


def test_provenance_is_strict_safe_and_display_name_does_not_change_identity():
    source = ExternalChatSource(external_chat_id="oc_dm", external_sender_id="ou_alice", display_name="小王")
    other = source.model_copy(update={"external_sender_id": "ou_bob"})
    assert source.safe_label != other.safe_label
    for changes in ({"platform": "slack"}, {"external_sender_id": "x\nsecret"},
                    {"display_name": "<script>"}, {"extra": True}):
        with pytest.raises(ValidationError):
            ExternalChatSource.model_validate({**source.model_dump(), **changes})
    event = inbound()
    assert event.fingerprint() == inbound(event_id="ev_other", display_name="新名称").fingerprint()
    with pytest.raises(ValidationError):
        FeishuInbound.model_validate({**event.model_dump(), "mentions_bot": "true"})
    for name in ("", "<script>", "name\u2066spoof"):
        with pytest.raises(ValidationError):
            inbound(display_name=name)
