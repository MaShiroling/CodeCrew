"""Official SDK receiver in one disposable child process, not a second orchestrator.

lark-oapi 1.7.3 has a global loop and blocking start(), but no public stop().
Isolation lets us confirm shutdown even if synchronous endpoint discovery hangs.
No Agent, SQLite operation or outgoing API call runs in the child.
"""

import asyncio
import multiprocessing
import queue
from collections.abc import Callable
from typing import Protocol

from pydantic import SecretStr

from app.feishu.adapter import FeishuAdapter
from app.feishu.models import ConnectionState, FeishuInbound
from app.feishu.privacy import log_event
from app.feishu.sender import require_sdk
from app.feishu.workers import finish_cleanup, stop_process


class FeishuTransport(Protocol):
    state: ConnectionState
    last_error: str | None
    receiver: Callable[[FeishuInbound], None] | None

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


def sdk_event_fields(event) -> dict:
    """Extract only permitted fields; never serialize the complete SDK object."""
    header, data = event.header, event.event
    message, sender = data.message, data.sender
    return {
        "header": {name: getattr(header, name, None)
                   for name in ("app_id", "event_id", "event_type")},
        "event": {
            "sender": {"sender_type": sender.sender_type,
                       "sender_id": {"open_id": sender.sender_id.open_id}},
            "message": {
                **{name: getattr(message, name, None) for name in
                   ("message_id", "chat_id", "chat_type", "message_type", "content")},
                "mentions": [{"key": item.key, "id": {"open_id": getattr(item.id, "open_id", None)}}
                             for item in message.mentions or []],
            },
        },
    }


def _sdk_worker(app_id: str, secret: str, bot_open_id: str, events, states) -> None:
    """Spawn entrypoint. SDK callbacks parse and enqueue in bounded time."""
    try:
        sdk = require_sdk()
        from lark_oapi.ws import client as ws_module

        adapter = FeishuAdapter(app_id, bot_open_id or None)

        def state(value: str) -> None:
            try:
                states.put_nowait(value)
            except queue.Full:
                pass

        def received(event) -> None:
            try:
                result = adapter.parse(sdk_event_fields(event))
            except (AttributeError, TypeError, ValueError):
                state("invalid_event")
                return
            if result.inbound is None:
                state(result.disposition)
                return
            try:
                events.put_nowait(result.inbound.model_dump())
            except queue.Full:
                state("ingress_queue_full")
                # SDK returns an error ACK when callbacks raise; do not pretend
                # an overflowing queue durably accepted a platform event.
                raise RuntimeError("ingress_queue_full") from None

        handler = sdk.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(
            received
        ).build()
        client = sdk.ws.Client(app_id, secret, event_handler=handler,
                               log_level=sdk.LogLevel.ERROR, auto_reconnect=True)
        client.on_reconnecting = lambda: state("reconnecting")
        client.on_reconnected = lambda: state("connected")

        async def observe_initial_connection():
            while client._conn is None:  # Narrow version-pinned observation, never lifecycle mutation.
                await asyncio.sleep(0.1)
            state("connected")

        ws_module.loop.set_exception_handler(lambda _loop, _context: state("sdk_background_failed"))
        ws_module.loop.create_task(observe_initial_connection())
        client.start()
    except BaseException:  # noqa: BLE001 - child exits without printing provider exceptions
        try:
            states.put_nowait("sdk_receiver_failed")
        except queue.Full:
            pass


class OfficialFeishuTransport:
    def __init__(self, app_id: str, app_secret: SecretStr, bot_open_id: str = "") -> None:
        self._app_id, self._secret, self._bot = app_id, app_secret, bot_open_id
        self.state = ConnectionState.DISABLED
        self.last_error: str | None = None
        self.receiver: Callable[[FeishuInbound], None] | None = None
        self._process = None
        self._pump_task: asyncio.Task | None = None

    async def start(self) -> None:
        require_sdk()
        if self._process is not None:
            raise ValueError("Feishu transport already started")
        context = multiprocessing.get_context("spawn")
        self._events = context.Queue(maxsize=128)
        self._states = context.Queue(maxsize=64)
        self.state = ConnectionState.STARTING
        self._process = context.Process(
            target=_sdk_worker, name="codecrew-feishu-receiver", daemon=True,
            args=(self._app_id, self._secret.get_secret_value(), self._bot, self._events, self._states),
        )
        try:
            self._process.start()
        except Exception:  # noqa: BLE001 - sanitize OS/process diagnostics too
            self._process.close()
            self._process = None
            for channel in (self._events, self._states):
                channel.cancel_join_thread()
                channel.close()
            self.state, self.last_error = ConnectionState.FAILED, "sdk_start_failed"
            raise RuntimeError("sdk_start_failed") from None
        self._pump_task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        try:
            await self._pump_events()
        except Exception:  # noqa: BLE001 - no raw queue, validation or callback error may escape
            self.state, self.last_error = ConnectionState.FAILED, "sdk_pump_failed"
            log_event("sdk_pump_failed")

    async def _pump_events(self) -> None:
        while True:
            try:
                for _ in range(64):
                    value = self._states.get_nowait()
                    if value in {"connected", "reconnecting"}:
                        self.state = ConnectionState(value)
                        log_event("connection_" + value)
                    else:
                        # Child messages are fixed code strings, not exception text.
                        self.last_error = value
                        if value in {"sdk_receiver_failed", "sdk_background_failed"}:
                            self.state = ConnectionState.FAILED
                        log_event("transport_diagnostic", error=value)
            except queue.Empty:
                pass
            try:
                for _ in range(32):
                    event = FeishuInbound.model_validate(self._events.get_nowait())
                    if self.receiver is not None:
                        self.receiver(event)
            except queue.Empty:
                pass
            if not self._process.is_alive():
                self.state = ConnectionState.FAILED
                self.last_error = "sdk_receiver_stopped"
                return
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        await finish_cleanup(self._stop())

    async def _stop(self) -> None:
        if self._pump_task is not None:
            self._pump_task.cancel()
            await asyncio.gather(self._pump_task, return_exceptions=True)
            self._pump_task = None
        if self._process is not None:
            try:
                await stop_process(self._process)
            except RuntimeError:
                self.state, self.last_error = ConnectionState.FAILED, "sdk_stop_unconfirmed"
                raise
            self._process = None
            for channel in (self._events, self._states):
                channel.cancel_join_thread()
                channel.close()
        self.state = ConnectionState.STOPPED


class FakeFeishuTransport:
    def __init__(self) -> None:
        self.state = ConnectionState.DISABLED
        self.last_error = None
        self.receiver = None
        self.starts = 0
        self.stops = 0

    async def start(self) -> None:
        self.starts += 1
        self.state = ConnectionState.CONNECTED

    async def stop(self) -> None:
        self.stops += 1
        self.state = ConnectionState.STOPPED

    def emit(self, event: FeishuInbound) -> None:
        if self.state is not ConnectionState.CONNECTED or self.receiver is None:
            raise RuntimeError("fake_transport_stopped")
        self.receiver(event)
