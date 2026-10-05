"""One Feishu lifetime, bounded ingress queue and durable outbox poller."""

import asyncio
import os

from app.config import Settings
from app.feishu.bridge import FeishuBridge
from app.feishu.models import FeishuInbound, FeishuStatus
from app.feishu.outbox import FeishuOutbox
from app.feishu.privacy import log_event
from app.feishu.sender import OfficialFeishuSender
from app.feishu.store import FeishuStore
from app.feishu.transport import OfficialFeishuTransport


class FeishuRuntime:
    def __init__(self, bridge: FeishuBridge, outbox: FeishuOutbox, transport) -> None:
        self.bridge, self.outbox, self.transport = bridge, outbox, transport
        self.transport.receiver = self.enqueue
        self._queue: asyncio.Queue[FeishuInbound] = asyncio.Queue(maxsize=128)
        self._tasks: list[asyncio.Task] = []
        self._accepting = False
        self._loop = None
        self.last_error: str | None = None

    async def start(self) -> None:
        if self._accepting:
            raise ValueError("Feishu runtime already started")
        self.bridge.settings.require_feishu()
        self.bridge.store.initialize()
        self.bridge.recover()
        self.outbox.store.recover_sending(force=True)
        self.outbox.scan()
        self._loop = asyncio.get_running_loop()
        self._accepting = True
        self._tasks = [asyncio.create_task(self._consume()), asyncio.create_task(self._deliver())]
        try:
            await self.transport.start()
        except BaseException:
            await self.stop()
            raise

    def enqueue(self, event: FeishuInbound) -> None:
        """Safe for a SDK callback thread: no DB, model, wait or provider data logging."""
        if self._accepting and self._loop is not None:
            self._loop.call_soon_threadsafe(self._enqueue_local, event)

    def _enqueue_local(self, event: FeishuInbound) -> None:
        if not self._accepting:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.last_error = "ingress_queue_full"
            log_event("ingress_queue_full", event=event.event_id)

    async def _consume(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self.bridge.receive(event)
            except Exception:  # noqa: BLE001 - keep original event and exception out of logs
                self.last_error = "ingress_failed"
                log_event("ingress_failed", event=event.event_id)
            finally:
                self._queue.task_done()

    async def _deliver(self) -> None:
        while self._accepting:
            try:
                self.outbox.scan()
                # A finite batch leaves time for inbound and shutdown.
                for _ in range(16):
                    if not await self.outbox.deliver_one():
                        break
            except Exception:  # noqa: BLE001
                self.last_error = "outbox_failed"
                log_event("outbox_failed")
            await asyncio.sleep(0.25)

    async def stop(self) -> None:
        self._accepting = False
        try:
            await self.transport.stop()
        finally:
            if self._tasks:
                try:
                    await asyncio.wait_for(self._queue.join(), timeout=5)
                except TimeoutError:
                    self.last_error = "ingress_shutdown_incomplete"
                self._tasks[0].cancel()
                # Allow an in-flight send to commit its receipt before stopping.
                try:
                    await asyncio.wait_for(asyncio.shield(self._tasks[1]), timeout=35)
                except (TimeoutError, asyncio.CancelledError):
                    self._tasks[1].cancel()
                await asyncio.gather(*self._tasks, return_exceptions=True)
                self._tasks = []

    def status(self) -> FeishuStatus:
        return FeishuStatus(enabled=True, connection_state=self.transport.state,
                            **self.bridge.store.counts(), last_error=(
                                self.last_error or self.transport.last_error or self.outbox.last_error))


def build_feishu_runtime(service, dispatcher, settings: Settings) -> FeishuRuntime:
    settings.require_feishu()
    store = FeishuStore(service.store, settings.feishu_app_id)
    store.initialize()
    bridge = FeishuBridge(service, dispatcher, store, settings)
    sender = OfficialFeishuSender(settings.feishu_app_id, settings.feishu_app_secret)
    transport = OfficialFeishuTransport(settings.feishu_app_id, settings.feishu_app_secret,
                                        settings.feishu_bot_open_id)
    # These values are used only to withhold accidental echoes, never persisted.
    secrets = tuple(value for key, value in os.environ.items() if value and (
        key.endswith(("_API_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN", "_APP_SECRET"))
    ))
    return FeishuRuntime(bridge, FeishuOutbox(store, sender, settings, secrets=secrets), transport)
