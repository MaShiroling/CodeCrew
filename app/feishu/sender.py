"""The only application-bot HTTP send boundary; SDK imports are optional."""

import asyncio
import importlib
import json
from importlib.metadata import PackageNotFoundError, version
from typing import Protocol

from pydantic import SecretStr

SDK_VERSION = "1.7.3"


def require_sdk():
    try:
        installed = version("lark-oapi")
        if installed != SDK_VERSION:
            raise ValueError("unsupported Feishu SDK version; install codecrew-ai[feishu]")
        sdk = importlib.import_module("lark_oapi")
    except (ImportError, PackageNotFoundError):
        raise ValueError("Feishu SDK is missing; run: pip install -e '.[feishu,dev]'") from None
    # SDK DEBUG can contain payloads, tokens and WS URLs; even ERROR can include
    # full platform IDs. Own fixed diagnostics replace all raw SDK log records.
    from lark_oapi.core.log import logger
    logger.disabled = True
    return sdk


class FeishuSender(Protocol):
    async def send_text(self, chat_id: str, text: str,
                        reply_to_message_id: str | None = None) -> str: ...


class FeishuSendError(RuntimeError):
    pass


class OfficialFeishuSender:
    def __init__(self, app_id: str, app_secret: SecretStr, *, client=None) -> None:
        self._sdk = require_sdk()
        self._client = client or self._sdk.Client.builder().app_id(app_id).app_secret(
            app_secret.get_secret_value()
        ).timeout(15).log_level(self._sdk.LogLevel.ERROR).build()

    async def send_text(self, chat_id: str, text: str,
                        reply_to_message_id: str | None = None) -> str:
        # The SDK's async entry still fetches tokens synchronously. Run the
        # entire sync call off the application loop, with its HTTP timeout set.
        try:
            return await asyncio.to_thread(self._send, chat_id, text, reply_to_message_id)
        except Exception:  # noqa: BLE001 - provider errors may contain credentials or payloads
            raise FeishuSendError("send_failed") from None

    def _send(self, chat_id: str, text: str, reply_to_message_id: str | None) -> str:
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        content = json.dumps({"text": text}, ensure_ascii=False)
        if reply_to_message_id:
            request = ReplyMessageRequest.builder().message_id(reply_to_message_id).request_body(
                ReplyMessageRequestBody.builder().msg_type("text").content(content)
                .reply_in_thread(False).build()
            ).build()
            response = self._client.im.v1.message.reply(request)
        else:
            request = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(
                CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("text")
                .content(content).build()
            ).build()
            response = self._client.im.v1.message.create(request)
        if not response.success() or response.data is None or not response.data.message_id:
            raise FeishuSendError("send_failed")
        return response.data.message_id


class FakeFeishuSender:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.attempts = 0
        self.sent: list[tuple[str, str, str | None]] = []

    async def send_text(self, chat_id: str, text: str,
                        reply_to_message_id: str | None = None) -> str:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise FeishuSendError("fake_temporary_failure")
        self.sent.append((chat_id, text, reply_to_message_id))
        return f"om_fake_{len(self.sent)}"
