"""Opt-in real Planner check for an explicitly continued long chat.

Only a disposable SQLite room and one Codex turn are used. Run with
CODECREW_RUN_CHAT_CONTEXT_LIVE=1 after configuring the Codex CLI in this shell.
"""

import asyncio
import json
import os
import shutil
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.chat.models import StoredStandaloneChatMessage
from app.cli import build_chat_app
from app.config import Settings

_TOKEN = "ANCHOR_BLUE_73"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("CODECREW_RUN_CHAT_CONTEXT_LIVE") != "1",
    reason="set CODECREW_RUN_CHAT_CONTEXT_LIVE=1 to spend one real Codex Planner turn",
)
async def test_real_planner_receives_explicit_old_human_context(tmp_path: Path) -> None:
    installed = Settings()
    if shutil.which(installed.codex_cli_path) is None:
        pytest.fail("Codex CLI unavailable; set CODECREW_CODEX_CLI_PATH in this terminal")
    settings = installed.model_copy(update={
        "database_url": f"sqlite:///{tmp_path / 'chat.sqlite3'}",
        "standalone_chat_workspace_root": tmp_path / "workspaces",
        "standalone_chat_runtime_root": tmp_path / "runtime",
    })
    app = build_chat_app(settings=settings)
    service = app.state.chat_service
    dispatcher = app.state.chat_dispatcher
    dispatcher.max_turns_per_thread = 1  # Bound live cost even if the model invites a teammate.
    captured_prompts: list[str] = []
    original_start = dispatcher.runtime.start

    async def capture_start(**kwargs):
        captured_prompts.append(kwargs["prompt"])
        return await original_start(**kwargs)

    dispatcher.runtime.start = capture_start
    await dispatcher.startup()
    room = None
    evidence: dict = {}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/v1/chats", json={
                "title": "P2.4 长聊真实验收", "idempotency_key": str(uuid4()),
            })
            assert created.status_code == 201
            room = created.json()
            room_id = UUID(room["room_id"])
            anchor: StoredStandaloneChatMessage = service.post_message(
                room_id,
                content=f"@白金 原始目标：讨论输入验证方案；验收代号 {_TOKEN}。只讨论，不改代码。",
                idempotency_key=uuid4(), reply_to=None,
            )
            for index in range(8):
                service.post_message(
                    room_id, content=f"@白金 无关旧话题 {index} " + "x" * 250,
                    idempotency_key=uuid4(), reply_to=None,
                )
            posted = await client.post(f"/api/v1/chats/{room_id}/messages", json={
                "content": (
                    "@白金 请从我显式选取的旧 Human 背景中找出验收代号，"
                    "只回答该代号和一句简短说明。不要读写文件或邀请其他 Agent。"
                ),
                "context_anchor_id": str(anchor.message.message_id),
                "idempotency_key": str(uuid4()),
            })
            assert posted.status_code == 201
            assert posted.json()["execution_authorized"] is False
            await asyncio.wait_for(dispatcher.wait_idle(), timeout=600)
            messages = (await client.get(f"/api/v1/chats/{room_id}/messages", params={
                "limit": 100,
            })).json()["items"]
            turns = (await client.get(f"/api/v1/chats/{room_id}/turns")).json()["items"]
            assert len(turns) == 1
            assert turns[0]["status"] == "succeeded", turns[0]["error"]
            assert len(captured_prompts) == 1
            assert _TOKEN in captured_prompts[0]
            assert "无关旧话题" not in captured_prompts[0]
            assert messages[-1]["message"]["context_anchor_id"] == str(
                anchor.message.message_id
            )
            assert _TOKEN in messages[-1]["message"]["content"]
            assert (await client.get("/api/v1/tasks")).status_code == 503
            assert not any(settings.standalone_chat_workspace_root.iterdir())
            assert not any(settings.standalone_chat_runtime_root.iterdir())
            evidence = {
                "trace_id": room["trace_id"], "room_id": room["room_id"],
                "anchor_id": str(anchor.message.message_id),
                "agent_turn_status": turns[0]["status"],
                "agent_reply": messages[-1]["message"]["content"],
                "token_found_in_prompt": True,
                "unrelated_history_in_prompt": False,
                "execution_authorized": False,
                "coding_task_api_unavailable": True,
            }
    finally:
        await dispatcher.shutdown()
        if room is not None:
            archive_root = Path(__file__).resolve().parents[2] / "evals/results/chat-context-live"
            archive_root.mkdir(parents=True, exist_ok=True)
            archive = archive_root / f"{room['trace_id']}.json"
            archive.write_text(json.dumps(evidence or {
                "trace_id": room["trace_id"], "incomplete": True,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"trace_id": room["trace_id"], "evidence": str(archive),
                              "acceptance_passed": bool(evidence)}, ensure_ascii=False))
