"""Manual opt-in: real WS + allowlisted DM/(optional) group + fixed text replies.

No model calls. Does not load .env, save credentials, or print raw identifiers.
See docs/feishu-setup.md before opting in. Runs only with explicit credentials.
"""

import asyncio
import os
import secrets

import pytest

from app.config import Settings
from app.feishu.bridge import FeishuBridge
from app.feishu.models import ConnectionState
from app.feishu.privacy import safe_identifier
from app.feishu.sender import SDK_VERSION, OfficialFeishuSender
from app.feishu.transport import OfficialFeishuTransport


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_long_connection_dm_group_and_reply():
    if os.environ.get("CODECREW_RUN_FEISHU_LIVE") != "1":
        pytest.skip("set CODECREW_RUN_FEISHU_LIVE=1 to opt in")
    required = ("CODECREW_FEISHU_APP_ID", "CODECREW_FEISHU_APP_SECRET",
                "CODECREW_FEISHU_ALLOWED_CHAT_IDS", "CODECREW_FEISHU_ALLOWED_SENDER_OPEN_IDS")
    if not all(os.environ.get(name, "").strip() for name in required):
        pytest.skip("live credentials/allowlists missing")
    pytest.importorskip("lark_oapi")
    settings = Settings(_env_file=None)
    settings.require_feishu()
    group = os.environ.get("CODECREW_FEISHU_LIVE_GROUP_CHAT_ID", "")
    if group and (group not in settings.feishu_allowed_chat_ids or not settings.feishu_bot_open_id):
        pytest.fail("live group needs bot identity and allowlist entry")
    token = "codecrew-smoke-" + secrets.token_hex(4)
    print(f"SDK {SDK_VERSION}; send DM containing {token}; group probe enabled={bool(group)}")
    queue = asyncio.Queue(maxsize=16)
    transport = OfficialFeishuTransport(settings.feishu_app_id, settings.feishu_app_secret, settings.feishu_bot_open_id)
    sender = OfficialFeishuSender(settings.feishu_app_id, settings.feishu_app_secret)
    # Reuse the same admission predicate without a DB, discussion or Agent runtime.
    gate = object.__new__(FeishuBridge)
    gate.settings = settings
    gate.store = type("AppIdentity", (), {"app_id": settings.feishu_app_id})()

    def receive(event):
        if gate.allowed(event) and token in event.text and not queue.full():
            queue.put_nowait(event)

    transport.receiver = receive
    observed = set()
    timeout = max(30, min(300, int(os.environ.get("CODECREW_FEISHU_LIVE_TIMEOUT", "120"))))
    try:
        await transport.start()
        async with asyncio.timeout(timeout):
            while "p2p" not in observed or (group and "group" not in observed):
                event = await queue.get()
                if event.chat_type == "group" and event.chat_id != group:
                    continue
                receipt = await sender.send_text(event.chat_id, "CodeCrew 飞书连接烟雾测试收到。", event.message_id)
                observed.add(event.chat_type)
                print(f"type={event.chat_type} chat={safe_identifier(event.chat_id)} "
                      f"message={safe_identifier(event.message_id)} receipt={safe_identifier(receipt)}")
        assert transport.state is ConnectionState.CONNECTED
    except TimeoutError:
        pytest.fail("live DM/group probe timed out; no raw provider diagnostics recorded")
    finally:
        await transport.stop()
    assert transport.state is ConnectionState.STOPPED
