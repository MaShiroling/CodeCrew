import asyncio
import queue
from types import SimpleNamespace

import pytest
from feishu_helpers import inbound
from pydantic import SecretStr

from app.feishu import transport as module
from app.feishu.models import ConnectionState


class Queue(queue.Queue):
    closed = False

    def cancel_join_thread(self):
        pass

    def close(self):
        self.closed = True


class Process:
    def __init__(self, **kwargs):
        self.alive, self.closed = False, False

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.alive = False

    def join(self, timeout):
        assert not self.alive

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_official_transport_queue_reconnect_and_confirmed_stop(monkeypatch):
    monkeypatch.setattr(module, "require_sdk", lambda: None)
    monkeypatch.setattr(module.multiprocessing, "get_context", lambda mode: SimpleNamespace(Queue=Queue, Process=Process))
    transport = module.OfficialFeishuTransport("cli_fake", SecretStr("fake-secret"))
    events = []
    transport.receiver = events.append
    await transport.start()
    process = transport._process
    for state in ("connected", "reconnecting", "connected"):
        transport._states.put_nowait(state)
        await asyncio.sleep(0.06)
        assert transport.state.value == state
    transport._events.put_nowait(inbound().model_dump())
    await asyncio.sleep(0.06)
    assert events == [inbound()]
    await transport.stop()
    assert not process.alive and process.closed and transport._pump_task is None
    assert transport._events.closed and transport._states.closed
    assert transport.state is ConnectionState.STOPPED


@pytest.mark.asyncio
async def test_process_start_failure_and_invalid_queue_data_are_sanitized(monkeypatch):
    class FailingProcess(Process):
        def start(self):
            raise OSError("sensitive-path-and-secret")

    monkeypatch.setattr(module, "require_sdk", lambda: None)
    monkeypatch.setattr(module.multiprocessing, "get_context", lambda mode: SimpleNamespace(Queue=Queue, Process=FailingProcess))
    transport = module.OfficialFeishuTransport("cli_fake", SecretStr("fake-secret"))
    with pytest.raises(RuntimeError, match="^sdk_start_failed$"):
        await transport.start()
    assert transport._process is None and transport._events.closed
    monkeypatch.setattr(module.multiprocessing, "get_context", lambda mode: SimpleNamespace(Queue=Queue, Process=Process))
    await transport.start()
    transport._events.put_nowait({"secret": "never-in-error"})
    await asyncio.sleep(0.06)
    assert transport.state is ConnectionState.FAILED and transport.last_error == "sdk_pump_failed"
    await transport.stop()
