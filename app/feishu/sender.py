"""The only application-bot HTTP send boundary; SDK imports are optional."""

import asyncio
import importlib
import json
import multiprocessing
import re
from importlib.metadata import PackageNotFoundError, version
from typing import Protocol

from pydantic import SecretStr

from app.feishu.workers import finish_cleanup, stop_process

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
    def __init__(self, app_id: str, app_secret: SecretStr) -> None:
        require_sdk()
        self._app_id, self._secret = app_id, app_secret
        self._lock = asyncio.Lock()
        self._process = None
        self.timeout_seconds = 30

    async def send_text(self, chat_id: str, text: str,
                        reply_to_message_id: str | None = None) -> str:
        # requests' socket timeout is not a wall deadline. A disposable child
        # bounds token lookup + send, and cancellation confirms its termination.
        async with self._lock:
            context = multiprocessing.get_context("spawn")
            reader, writer = context.Pipe(duplex=False)
            process = context.Process(
                target=_sdk_send_worker, name="codecrew-feishu-sender", daemon=True,
                args=(self._app_id, self._secret.get_secret_value(), chat_id, text,
                      reply_to_message_id, writer),
            )
            self._process = process
            try:
                process.start()
                writer.close()
                async with asyncio.timeout(self.timeout_seconds):
                    while not reader.poll():
                        if not process.is_alive():
                            raise FeishuSendError("send_failed")
                        await asyncio.sleep(0.02)
                    receipt = reader.recv()
                    if not isinstance(receipt, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,255}", receipt):
                        raise FeishuSendError("send_failed")
                    return receipt
            except Exception:  # noqa: BLE001 - no provider or OS diagnostics escape
                raise FeishuSendError("send_failed") from None
            finally:
                await finish_cleanup(self._cleanup(process, reader, writer))

    async def _cleanup(self, process, reader, writer) -> None:
        try:
            if process.pid is None:
                process.close()
            else:
                await stop_process(process)
        finally:
            reader.close()
            writer.close()
        self._process = None


def _sdk_send_worker(app_id, secret, chat_id, text, reply_to_message_id, result) -> None:
    try:
        sdk = require_sdk()
        client = sdk.Client.builder().app_id(app_id).app_secret(secret).timeout(15).log_level(
            sdk.LogLevel.ERROR
        ).build()
        receipt = _send_request(client, chat_id, text, reply_to_message_id)
    except BaseException:  # noqa: BLE001 - discard the raw SDK exception before IPC
        receipt = None
    try:
        result.send(receipt)
    except (BrokenPipeError, EOFError, OSError):
        # Parent cancellation may close IPC first. Never print a traceback with
        # the original provider exception as its context in the child process.
        pass
    finally:
        result.close()


def _send_request(client, chat_id: str, text: str, reply_to_message_id: str | None) -> str:
    try:
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
            response = client.im.v1.message.reply(request)
        else:
            request = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(
                CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("text")
                .content(content).build()
            ).build()
            response = client.im.v1.message.create(request)
        if not response.success() or response.data is None or not response.data.message_id:
            raise FeishuSendError("send_failed")
        return response.data.message_id
    except Exception:  # noqa: BLE001
        raise FeishuSendError("send_failed") from None


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
