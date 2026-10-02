"""P1: standalone browser entry and credential-free Fake team acceptance."""

import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import cli
from app.cli import build_chat_app
from app.config import Settings
from app.main import create_app


def test_standalone_chat_ui_is_separate_from_task_workspace() -> None:
    with TestClient(create_app()) as client:
        page = client.get("/ui/chat/")
        assert page.status_code == 200
        assert "团队聊天室" in page.text
        assert 'id="create-room-form"' in page.text
        assert 'id="message-form"' in page.text
        assert 'data-mention="@白金"' in page.text
        assert 'data-mention="@月见"' in page.text
        assert 'data-mention="@鲸鲸"' in page.text
        assert 'id="turn-list"' in page.text
        assert 'id="reply-preview"' in page.text
        assert 'id="coding-panel"' in page.text
        assert 'id="coding-confirm"' in page.text
        assert 'id="coding-task-link"' in page.text
        assert '/ui/assets/avatars.js' in page.text
        assert '/ui/assets/avatars.css' in page.text
        assert "create-repository" not in page.text
        assert client.get("/ui/chat").status_code == 200
        assert 'href="/ui/chat/"' in client.get("/ui/").text
        javascript = client.get("/ui/assets/chat.js")
        assert javascript.status_code == 200
        assert "/api/v1/chats" in javascript.text
        assert "/api/v1/tasks" not in javascript.text
        assert "/coding-task-preflight" in javascript.text
        assert "/coding-tasks" in javascript.text
        stylesheet = client.get("/ui/assets/chat.css")
        assert stylesheet.status_code == 200
        assert "@media(max-width:700px)" in stylesheet.text
        for role in ("planner", "implementer", "reviewer"):
            portrait = client.get(f"/ui/assets/avatars/{role}.jpg")
            assert portrait.status_code == 200
            assert portrait.headers["content-type"].startswith("image/jpeg")
            assert portrait.content.startswith(b"\xff\xd8\xff")
        workspace = client.get("/ui/")
        assert '/ui/assets/avatars.js' in workspace.text
        assert '/ui/assets/avatars.css' in workspace.text


def test_standalone_chat_browser_script_with_mock_api() -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is not installed; browser script harness unavailable")
    script = Path(__file__).with_name("ui_chat.test.cjs")
    subprocess.run(["node", "--check", str(script)], check=True)
    subprocess.run(["node", str(script)], check=True)


def test_chat_demo_cli_needs_no_real_agent_credentials(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'chat.sqlite3'}",
        standalone_chat_workspace_root=tmp_path / "chat-workspaces",
        standalone_chat_runtime_root=tmp_path / "chat-runtime",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    monkeypatch.delenv("KIMI_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **options: calls.append((app, options)))

    assert cli.main(["chat-demo", "--port", "8767"]) == 0
    assert calls[0][1] == {"host": "127.0.0.1", "port": 8767, "workers": 1}
    assert not hasattr(calls[0][0].state, "task_service")


def test_fake_chat_demo_runs_three_agents_and_survives_restart(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'chat.sqlite3'}",
        standalone_chat_workspace_root=tmp_path / "chat-workspaces",
        standalone_chat_runtime_root=tmp_path / "chat-runtime",
    )
    with TestClient(build_chat_app(settings=settings, fake_agents=True)) as client:
        assert client.get("/ui/chat/").status_code == 200
        created = client.post("/api/v1/chats", json={
            "title": "Fake 三角色讨论", "idempotency_key": str(uuid4()),
        })
        assert created.status_code == 201
        room_id = created.json()["room_id"]
        sent = client.post(f"/api/v1/chats/{room_id}/messages", json={
            "content": "@白金 请和队友讨论输入校验", "idempotency_key": str(uuid4()),
        })
        assert sent.status_code == 201
        assert sent.json()["execution_authorized"] is False
        for _ in range(100):
            turns = client.get(f"/api/v1/chats/{room_id}/turns").json()["items"]
            if len(turns) == 3 and all(item["status"] == "succeeded" for item in turns):
                break
            time.sleep(0.01)
        assert [item["status"] for item in turns] == ["succeeded"] * 3
        messages = client.get(f"/api/v1/chats/{room_id}/messages").json()["items"]
        assert len(messages) == 4
        assert [item["message"]["content"].split("：")[0] for item in messages[1:]] == [
            "白金", "月见", "鲸鲸",
        ]
        assert client.get("/api/v1/tasks").status_code == 503
    with TestClient(build_chat_app(settings=settings, fake_agents=True)) as client:
        assert client.get("/api/v1/chats").json()["items"][0]["room_id"] == room_id
        assert len(client.get(f"/api/v1/chats/{room_id}/messages").json()["items"]) == 4
