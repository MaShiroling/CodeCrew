"""Optional official SDK contract checks; never use a network or real credentials."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.feishu.adapter import FeishuAdapter
from app.feishu.sender import SDK_VERSION, FeishuSendError, OfficialFeishuSender, require_sdk
from app.feishu.transport import sdk_event_fields


@pytest.fixture
def sdk():
    pytest.importorskip("lark_oapi")
    return require_sdk()


@pytest.mark.asyncio
async def test_pinned_official_event_and_ws_lifecycle_contract(sdk):
    from importlib.metadata import version

    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
    from test_feishu_adapter import payload
    raw = payload(chat_type="group", content=json.dumps({"text": "@_user_1 @白金 讨论"}),
                  mentions=[{"key": "@_user_1", "id": {"open_id": "ou_bot"}}])
    typed = P2ImMessageReceiveV1(raw)
    parsed = FeishuAdapter("cli_test", "ou_bot").parse(sdk_event_fields(typed))
    assert parsed.inbound.text == "@白金 讨论"
    client = sdk.ws.Client("cli_fake", "fake-secret", auto_reconnect=True)
    assert version("lark-oapi") == SDK_VERSION
    assert hasattr(client, "_conn") and callable(client.start)
    assert callable(client.on_reconnecting) and callable(client.on_reconnected)
    from lark_oapi.core.log import logger
    assert logger.disabled
    # Test-only cleanup of the inspected SDK cache cron; production WS runs in
    # its disposable receiver process because the SDK has no public stop API.
    client._cache._cron.cancel()
    await asyncio.gather(client._cache._cron, return_exceptions=True)


@pytest.mark.asyncio
async def test_official_create_and_reply_request_contract_and_error_redaction(sdk):
    calls = []

    def accept(request):
        calls.append(request)
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="om_receipt"))

    methods = SimpleNamespace(create=accept, reply=accept)
    client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=methods)))
    sender = OfficialFeishuSender("cli_fake", SecretStr("fake-secret"), client=client)
    assert await sender.send_text("oc_test", "文本") == "om_receipt"
    assert calls[0].receive_id_type == "chat_id"
    assert calls[0].request_body.receive_id == "oc_test"
    assert json.loads(calls[0].request_body.content) == {"text": "文本"}
    assert await sender.send_text("oc_test", "reply", "om_original") == "om_receipt"
    assert calls[1].message_id == "om_original" and calls[1].request_body.reply_in_thread is False

    def fail(_request):
        raise RuntimeError("token-and-provider-payload-must-not-escape")

    methods.reply = fail
    with pytest.raises(FeishuSendError, match="^send_failed$") as error:
        await sender.send_text("oc_test", "retry", "om_original")
    assert error.value.__suppress_context__ is True
    assert len(calls) == 2  # no unsafe fallback send to another destination
