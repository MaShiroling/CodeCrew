import asyncio
import importlib
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from feishu_helpers import inbound, requests, rows, setup

from app import cli
from app.config import Settings
from app.feishu.models import ConnectionState
from app.feishu.sender import require_sdk
from app.main import create_app


@pytest.mark.asyncio
async def test_callback_from_thread_reconnect_and_shutdown(tmp_path):
    h = setup(tmp_path)
    await h.dispatcher.startup()
    await h.runtime.start()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(h.transport.emit, inbound()).result(timeout=2)
    for _ in range(200):
        if rows(h, "feishu_ingress"):
            break
        await asyncio.sleep(0.01)
    await h.dispatcher.wait_idle()
    h.transport.state = ConnectionState.RECONNECTING
    assert h.runtime.status().connection_state is ConnectionState.RECONNECTING
    h.transport.state = ConnectionState.CONNECTED
    h.transport.emit(inbound())
    await asyncio.sleep(0.02)
    assert len(requests(h)) == 3
    await h.runtime.stop()
    await h.dispatcher.shutdown()
    assert h.runtime._tasks == [] and h.transport.stops == 1
    with pytest.raises(RuntimeError, match="stopped"):
        h.transport.emit(inbound(2))
    h.runtime.enqueue(inbound(3))
    await asyncio.sleep(0)
    assert len(rows(h, "feishu_ingress")) == 1


def test_lifespan_status_api_identity_and_web_cannot_spoof_provenance(tmp_path):
    h = setup(tmp_path)
    room = h.service.create_room(title="API", idempotency_key=uuid4())
    from app.chat.models import ExternalChatSource
    source = h.service.post_external_message(room.room_id, content="hello", idempotency_key=uuid4(),
        external_source=ExternalChatSource(external_chat_id="oc_dm", external_sender_id="ou_alice", display_name="Alice"))
    app = create_app(chat_service=h.service, bounded_dispatcher=h.dispatcher, feishu_runtime=h.runtime)
    with TestClient(app) as client:
        status = client.get("/api/v1/feishu/status").json()
        assert status["enabled"] is True and status["connection_state"] == "connected"
        assert set(status) == {"enabled", "connection_state", "binding_count", "pending_outbox_count", "retry_count", "failed_count", "last_error"}
        page = client.get(f"/api/v1/chats/{room.room_id}/messages").json()
        assert page["items"][0]["message"]["external_source"]["external_sender_id"] == "ou_alice"
        assert client.post(f"/api/v1/chats/{room.room_id}/messages", json={
            "idempotency_key": str(uuid4()), "content": "@白金 fake", "external_source": source.message.external_source.model_dump(),
        }).status_code == 422
        assert client.get("/ui/assets/feishu.js").status_code == 200
    assert h.transport.starts == h.transport.stops == 1
    with TestClient(create_app(chat_service=h.service)) as client:
        status = client.get("/api/v1/feishu/status").json()
        assert status["enabled"] is False and status["connection_state"] == "disabled"


@pytest.mark.asyncio
async def test_start_failure_cleans_up_and_full_queue_is_observable(tmp_path, monkeypatch):
    h = setup(tmp_path)

    async def fail_start():
        raise RuntimeError("fixed failure")

    monkeypatch.setattr(h.transport, "start", fail_start)
    with pytest.raises(RuntimeError):
        await h.runtime.start()
    assert h.runtime._tasks == [] and not h.runtime._accepting and h.transport.stops == 1
    h.runtime._accepting = True
    for n in range(129):
        h.runtime._enqueue_local(inbound(n))
    assert h.runtime.last_error == "ingress_queue_full" and h.runtime._queue.qsize() == 128
    h.runtime._accepting = False


@pytest.mark.parametrize("overrides", [
    {"feishu_enabled": False}, {"feishu_app_id": ""}, {"feishu_app_secret": ""},
    {"feishu_allowed_chat_ids": frozenset()}, {"feishu_allowed_sender_open_ids": frozenset()},
    {"feishu_retry_base_seconds": 5, "feishu_retry_cap_seconds": 1},
])
def test_explicit_start_validates_fail_closed(tmp_path, overrides):
    h = setup(tmp_path, **overrides)
    with pytest.raises(ValueError):
        h.settings.require_feishu()


def test_ordinary_chat_and_demo_do_not_load_sdk_even_when_env_enabled(tmp_path, monkeypatch):
    h = setup(tmp_path)
    monkeypatch.setattr(cli, "_build_chat_service", lambda settings: h.service)
    monkeypatch.setattr(cli, "build_standalone_chat_agent_runtime", lambda settings: h.agents)
    monkeypatch.setattr(cli, "_build_fake_chat_runtime", lambda settings: h.agents)
    monkeypatch.setattr(cli, "_build_fake_bounded_runtime", lambda settings: h.agents)
    sdk = importlib.import_module("app.feishu.sender")
    monkeypatch.setattr(sdk, "require_sdk", lambda: pytest.fail("unexpected SDK import/start"))
    for fake in (False, True):
        with TestClient(cli.build_chat_app(settings=h.settings, fake_agents=fake)) as client:
            assert client.get("/api/v1/feishu/status").json()["enabled"] is False
    with pytest.raises(ValueError, match="chat-demo"):
        cli.build_chat_app(settings=h.settings, fake_agents=True, feishu=True)
    with pytest.raises(ValueError, match="ENABLED"):
        cli.build_chat_app(settings=Settings(_env_file=None), feishu=True)


def test_sdk_missing_has_clear_error_without_importing_it(monkeypatch):
    from importlib.metadata import PackageNotFoundError
    sdk = importlib.import_module("app.feishu.sender")

    def missing(_name):
        raise PackageNotFoundError()

    monkeypatch.setattr(sdk, "version", missing)
    with pytest.raises(ValueError, match="pip install"):
        require_sdk()


def test_cli_flag_only_on_chat_serve(monkeypatch):
    called = []
    marker = object()
    monkeypatch.setattr(cli, "build_chat_app", lambda **kwargs: called.append(kwargs) or marker)
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: called.append(kwargs))
    assert cli.main(["chat-serve", "--feishu"]) == 0
    assert called[0]["feishu"] is True
    assert called[1]["workers"] == 1 and called[1]["host"] == "127.0.0.1"
    with pytest.raises(SystemExit):
        cli.main(["chat-demo", "--feishu"])


def test_lifespan_rejects_task_runtime_with_feishu(tmp_path):
    h = setup(tmp_path)
    with pytest.raises(ValueError, match="chat-only"):
        create_app(chat_service=h.service, bounded_dispatcher=h.dispatcher, feishu_runtime=h.runtime, task_service=object())
