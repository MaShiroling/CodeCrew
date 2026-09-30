"""Opt-in, repository-free three-model chat smoke through the real HTTP API.

Run with CODECREW_RUN_STANDALONE_CHAT_LIVE=1 and both model keys in the same
terminal. This can spend up to six real model turns. No coding Task is created.
"""

import asyncio
import json
import os
import platform
import shutil
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.cli import build_chat_app
from app.config import Settings


def _preflight() -> Settings:
    if platform.system() != "Darwin":
        pytest.fail("standalone live chat requires macOS Seatbelt for Kimi")
    settings = Settings()
    for name, command in (
        ("CODECREW_CODEX_CLI_PATH", settings.codex_cli_path),
        ("CODECREW_KIMI_CLI_PATH", settings.kimi_cli_path),
        ("CODECREW_CLAUDE_CLI_PATH", settings.claude_cli_path),
    ):
        if shutil.which(command) is None:
            pytest.fail(f"CLI unavailable at {command!r}; set {name} in this terminal")
    for name in ("KIMI_MODEL_API_KEY", "DEEPSEEK_API_KEY"):
        if not os.environ.get(name, "").strip():
            pytest.fail(f"set {name} in this terminal; never put the key in a command")
    return settings


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("CODECREW_RUN_STANDALONE_CHAT_LIVE") != "1",
    reason="set CODECREW_RUN_STANDALONE_CHAT_LIVE=1 to spend up to six real model turns",
)
async def test_three_real_agents_reply_in_standalone_room(tmp_path: Path) -> None:
    installed = _preflight()
    settings = installed.model_copy(update={
        "database_url": f"sqlite:///{tmp_path / 'chat.sqlite3'}",
        "standalone_chat_workspace_root": tmp_path / "chat-workspaces",
        "standalone_chat_runtime_root": tmp_path / "chat-runtime",
    })
    app = build_chat_app(settings=settings)
    dispatcher = app.state.chat_dispatcher
    await dispatcher.startup()
    room = None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/v1/chats", json={
                "title": "三模型只读讨论验收", "idempotency_key": str(uuid4()),
            })
            assert created.status_code == 201
            room = created.json()
            room_id = UUID(room["room_id"])
            posted = await client.post(f"/api/v1/chats/{room_id}/messages", json={
                "content": (
                    "@白金 @月见 @鲸鲸 请各用一两句话讨论：给 Python 函数增加输入校验时，"
                    "如何兼顾兼容性与测试？只回复 Human，不要再邀请队友接话；"
                    "不要读写文件或执行命令。"
                ),
                "idempotency_key": str(uuid4()),
            })
            assert posted.status_code == 201
            assert posted.json()["execution_authorized"] is False
            await asyncio.wait_for(dispatcher.wait_idle(), timeout=1800)
            messages = (await client.get(f"/api/v1/chats/{room_id}/messages", params={
                "limit": 100,
            })).json()["items"]
            turns = (await client.get(f"/api/v1/chats/{room_id}/turns")).json()["items"]
            assert len(turns) >= 3
            assert all(turn["status"] == "succeeded" for turn in turns), [
                (turn["status"], turn["error"]) for turn in turns
            ]
            sender_roles = {
                next(member["role"] for member in room["members"]
                     if member["member_id"] == item["message"]["sender_id"])
                for item in messages
            }
            assert {"planner", "implementer", "reviewer"}.issubset(sender_roles)
            assert (await client.get("/api/v1/tasks")).status_code == 503
            assert not any(settings.standalone_chat_workspace_root.iterdir())
            assert not any(settings.standalone_chat_runtime_root.iterdir())
    finally:
        await dispatcher.shutdown()
        if room is not None:
            room_id = UUID(room["room_id"])
            messages = [
                item.model_dump(mode="json")
                for item in app.state.chat_service.store.list_messages(
                    room_id, after_sequence=0, limit=100,
                )
            ]
            turns = [
                item.model_dump(mode="json")
                for item in app.state.chat_service.store.list_turns(room_id)
            ]
            archive_root = Path(__file__).resolve().parents[2] / "evals/results/standalone-chat-live"
            archive_root.mkdir(parents=True, exist_ok=True)
            archive = archive_root / f"{room['trace_id']}.json"
            archive.write_text(json.dumps({
                "room": room, "messages": messages, "turns": turns,
                "execution_authorized": False,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({
                "trace_id": room["trace_id"], "evidence": str(archive),
                "turn_statuses": [turn["status"] for turn in turns],
                "message_count": len(messages),
            }, ensure_ascii=False))
