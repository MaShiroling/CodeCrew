"""P6.6: opt-in three-model, sequential, repository-free discussion acceptance.

The real case spends at most three Agent turns and is skipped by default.
Its JSON evidence is kept under ignored evals/results, never in Git.
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

_LIVE_FLAG = "CODECREW_RUN_BOUNDED_DISCUSSION_LIVE"
_TERMINAL_STOPS = {
    ("finished", "agent_finished"),
    ("awaiting_human", "human_input_needed"),
    ("limit_reached", "turn_limit"),
}


def _preflight() -> Settings:
    if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        pytest.fail("real bounded chat requires macOS Seatbelt for Kimi")
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


def _check_acceptance(room: dict, run: dict, messages: list[dict], turns: list[dict]) -> None:
    """Validate persisted evidence, not any Agent's claim that the chat succeeded."""
    members = {member["member_id"]: member["role"] for member in room["members"]}
    root = messages[0]["message"]
    agent_messages = [item["message"] for item in messages[1:]]
    assert (run["status"], run["stop_reason"]) in _TERMINAL_STOPS, run
    assert run["agent_turns_used"] == len(turns) == 3
    assert run["agent_turns_used"] <= run["limits"]["max_agent_turns"]
    assert all(turn["status"] == "succeeded" for turn in turns), turns
    assert [members[turn["recipient_id"]] for turn in turns] == [
        "planner", "implementer", "reviewer",
    ]
    assert [members[item["sender_id"]] for item in agent_messages] == [
        "planner", "implementer", "reviewer",
    ]
    assert len(agent_messages) == 3
    assert all(item["correlation_id"] == run["correlation_id"] for item in agent_messages)
    assert all(item["reply_to"] == item["causation_id"] for item in agent_messages)
    assert all(item["content"].strip() for item in agent_messages)
    assert agent_messages[0]["reply_to"] == root["message_id"]
    assert agent_messages[1]["reply_to"] == agent_messages[0]["message_id"]
    assert agent_messages[2]["reply_to"] in {
        agent_messages[0]["message_id"], agent_messages[1]["message_id"],
    }


async def _exercise(app, settings: Settings, *, archive: bool) -> None:
    bounded = app.state.bounded_dispatcher
    room = None
    room_id = None
    run_id = None
    await app.state.chat_dispatcher.startup()
    await bounded.startup()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/v1/chats", json={
                "title": "P6.6 三模型有界只读接话", "idempotency_key": str(uuid4()),
            })
            assert created.status_code == 201, created.text
            room = created.json()
            room_id = UUID(room["room_id"])
            path = f"/api/v1/chats/{room_id}/discussion-runs"
            started = await client.post(path, json={
                "idempotency_key": str(uuid4()),
                "opening_role": "planner",
                "limits": {"max_agent_turns": 3, "max_elapsed_seconds": 600},
                "content": (
                    "只读讨论：给一个小型开源项目的三人协作团队各起一个简短的代号。"
                    "请白金先给方案，并在结构化交接中按月见、鲸鲸的顺序邀请两人；"
                    "月见补充实现者视角，鲸鲸最后指出命名风险并结束本批。"
                    "每人一两句话即可。不读取文件、不运行命令、不创建编码任务。"
                ),
            })
            assert started.status_code == 201, started.text
            assert started.json()["execution_authorized"] is False
            run_id = UUID(started.json()["run"]["run_id"])
            await asyncio.wait_for(bounded.wait_idle(), timeout=630)
            run_response = await client.get(f"{path}/{run_id}")
            assert run_response.status_code == 200, run_response.text
            run = run_response.json()
            messages_response = await client.get(
                f"/api/v1/chats/{room_id}/messages", params={"limit": 100},
            )
            turns_response = await client.get(f"/api/v1/chats/{room_id}/turns")
            assert messages_response.status_code == turns_response.status_code == 200
            messages = messages_response.json()["items"]
            turns = turns_response.json()["items"]
            _check_acceptance(room, run, messages, turns)
            assert (await client.get("/api/v1/tasks")).status_code == 503
            assert not any(settings.standalone_chat_workspace_root.iterdir())
            assert not any(settings.standalone_chat_runtime_root.iterdir())
    finally:
        await bounded.shutdown()
        await app.state.chat_dispatcher.shutdown()
        if archive and room is not None:
            store = app.state.chat_service.store
            archived_run = (
                bounded.runs.get(run_id).model_dump(mode="json") if run_id else None
            )
            archived_messages = [
                item.model_dump(mode="json")
                for item in store.list_messages(room_id, after_sequence=0, limit=100)
            ]
            archived_turns = [
                item.model_dump(mode="json") for item in store.list_turns(room_id)
            ]
            archive_root = Path(__file__).resolve().parents[2] / "evals/results/bounded-discussion-live"
            archive_root.mkdir(parents=True, exist_ok=True)
            evidence = archive_root / f"{room['trace_id']}.json"
            evidence.write_text(json.dumps({
                "room": room, "run": archived_run, "messages": archived_messages,
                "turns": archived_turns, "execution_authorized": False,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({
                "trace_id": room["trace_id"], "evidence": str(evidence),
                "run_status": archived_run["status"] if archived_run else None,
                "stop_reason": archived_run["stop_reason"] if archived_run else None,
                "agent_turns_used": archived_run["agent_turns_used"] if archived_run else None,
                "turn_statuses": [turn["status"] for turn in archived_turns],
            }, ensure_ascii=False))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bounded_discussion_acceptance_with_fake_agents(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'chat.sqlite3'}",
        standalone_chat_workspace_root=tmp_path / "workspaces",
        standalone_chat_runtime_root=tmp_path / "runtime",
    )
    await _exercise(build_chat_app(settings=settings, fake_agents=True), settings, archive=False)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv(_LIVE_FLAG) != "1",
    reason=f"set {_LIVE_FLAG}=1 to spend at most three real model turns",
)
async def test_three_real_agents_handoff_in_bounded_room(tmp_path: Path) -> None:
    installed = _preflight()
    settings = installed.model_copy(update={
        "database_url": f"sqlite:///{tmp_path / 'chat.sqlite3'}",
        "standalone_chat_workspace_root": tmp_path / "workspaces",
        "standalone_chat_runtime_root": tmp_path / "runtime",
    })
    await _exercise(build_chat_app(settings=settings), settings, archive=True)
