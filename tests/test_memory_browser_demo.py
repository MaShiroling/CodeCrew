"""Scripted UI fixture uses real persistence, dispatch and recall without keys."""

import json
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from scripts.memory_browser_demo import CANCEL, COMPLETE, PREFERENCE, TODO, build_memory_demo_app


def test_memory_demo_refuses_existing_unrelated_directory(tmp_path):
    (tmp_path / "keep.txt").write_text("user data", encoding="utf-8")
    with pytest.raises(ValueError, match="new empty"):
        build_memory_demo_app(tmp_path)
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "user data"


@pytest.mark.asyncio
@pytest.mark.parametrize("closure,state", [(COMPLETE, "completed"), (CANCEL, "cancelled")])
async def test_memory_demo_real_write_recall_close_and_restart(tmp_path, monkeypatch, closure, state):
    # Invalid inherited cloud settings must not activate or break the fixture.
    monkeypatch.setenv("CODECREW_MEMORY_EMBEDDING_MODE", "on")
    monkeypatch.setenv("CODECREW_MEMORY_EMBEDDING_BASE_URL", "invalid")
    app = build_memory_demo_app(tmp_path)
    dispatcher = app.state.chat_dispatcher
    await dispatcher.startup()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://demo") as client:
            room = (await client.post("/api/v1/chats", json={
                "title": "Fake 记忆演示", "idempotency_key": str(uuid4()),
            })).json()
            base = f"/api/v1/chats/{room['room_id']}"

            async def send(content):
                receipt = await client.post(f"{base}/messages", json={
                    "content": f"@白金 {content}", "idempotency_key": str(uuid4()),
                })
                assert receipt.status_code == 201
                assert receipt.json()["execution_authorized"] is False
                await dispatcher.wait_idle()
                return receipt.json()["message"]["message"]

            source = await send(PREFERENCE)
            await send("本房间回答要用什么语言，先给什么？")
            adapter = dispatcher.runtime.adapters[next(iter(dispatcher.runtime.adapters))]
            prefix = "上下文摘录（JSON）："
            line = next(line for line in adapter.requests[-1].prompt.splitlines()
                        if line.startswith(prefix))
            context = json.loads(line[len(prefix):])
            assert context["history"] == []
            assert context["room_memories"][0]["source_message_id"] == source["message_id"]
            await send(TODO)
            await send(closure)
            page = (await client.get(f"{base}/memories?include_closed=true")).json()["items"]
            assert len(page) == 2
            closed = next(item for item in page if item["kind"] == "open_loop")
            assert closed["open_loop_state"] == state
            assert closed["closed_by_message_id"]
            request_count = len(adapter.requests)
            await send("文档链接检查还在待办吗？")
            assert len(adapter.requests) == request_count  # SQLite receipt, not Fake prose.
            preference = next(item for item in page if item["kind"] == "preference")
            retired = await client.delete(f"{base}/memories/{preference['memory_id']}")
            assert retired.status_code == 200
            assert retired.json()["status"] == "retired"
            await send("本房间回答要用什么语言，先给什么？")
            line = next(line for line in adapter.requests[-1].prompt.splitlines()
                        if line.startswith(prefix))
            assert json.loads(line[len(prefix):])["room_memories"] == []
    finally:
        await dispatcher.shutdown()

    restarted = build_memory_demo_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=restarted), base_url="http://demo") as client:
        active = (await client.get(f"{base}/memories")).json()["items"]
        assert active == []
        closed_page = (await client.get(f"{base}/memories?include_closed=true")).json()["items"]
        assert next(item for item in closed_page if item["kind"] == "open_loop") == closed
