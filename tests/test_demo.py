"""P3.4: one disposable Fake browser/API demo, no model credentials."""

import time
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app import cli
from app.demo import DEMO_ISSUE, build_demo_app


def test_demo_chat_to_code_delivery_is_explicit_and_fixture_bound(tmp_path: Path) -> None:
    app, repository = build_demo_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/ui/chat/").status_code == 200
        assert client.get("/ui/").status_code == 200
        assert client.get("/api/v1/chats/coding-capability").json() == {
            "available": True, "allowed_paths": ["src"],
            "demo_repository_path": str(repository),
            "demo_issue": DEMO_ISSUE,
        }
        room = client.post("/api/v1/chats", json={
            "title": "Fake 小修复", "idempotency_key": str(uuid4()),
        }).json()
        room_id = room["room_id"]
        sent = client.post(f"/api/v1/chats/{room_id}/messages", json={
            "content": "@白金 先讨论将 src/app.py 的 value 改为 2",
            "idempotency_key": str(uuid4()),
        })
        assert sent.status_code == 201
        source = sent.json()["message"]["message"]["message_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            turns = client.get(f"/api/v1/chats/{room_id}/turns").json()["items"]
            if len(turns) == 3 and all(turn["status"] == "succeeded" for turn in turns):
                break
            time.sleep(0.01)
        assert len(turns) == 3
        assert all(turn["status"] == "succeeded" for turn in turns)
        assert client.get("/api/v1/tasks").json()["items"] == []
        assert (repository / "src/app.py").read_text() == "value = 1\n"
        assert not (tmp_path / "worktrees").exists()

        draft = {
            "source_message_id": source, "repository_path": str(repository),
            "issue": DEMO_ISSUE, "allowed_paths": ["src"],
        }
        another_repository = tmp_path / "other-repository"
        another_repository.mkdir()
        assert client.post(f"/api/v1/chats/{room_id}/coding-task-preflight", json={
            **draft, "repository_path": str(another_repository),
        }).status_code == 409
        assert client.post(f"/api/v1/chats/{room_id}/coding-task-preflight", json={
            **draft, "issue": "unrelated request",
        }).status_code == 409
        preview = client.post(f"/api/v1/chats/{room_id}/coding-task-preflight", json=draft)
        assert preview.status_code == 200, preview.text
        assert preview.json()["execution_authorized"] is False
        assert client.get("/api/v1/tasks").json()["items"] == []
        assert (repository / "src/app.py").read_text() == "value = 1\n"
        assert client.post("/api/v1/tasks", json={
            "issue": "bypass demo", "repository_path": str(repository),
        }).status_code == 503

        command = {
            **draft, "expected_base_commit": preview.json()["base_commit"],
            "idempotency_key": str(uuid4()), "confirmation": "authorize_one_coding_task",
        }
        assert client.post(f"/api/v1/chats/{room_id}/coding-tasks", json={
            **command, "confirmation": "no",
        }).status_code == 422
        assert client.post(f"/api/v1/chats/{room_id}/coding-tasks", json={
            **command, "repository_path": str(another_repository),
        }).status_code == 409
        created = client.post(f"/api/v1/chats/{room_id}/coding-tasks", json=command)
        assert created.status_code == 201, created.text
        task_id = created.json()["task_id"]
        assert client.post(f"/api/v1/chats/{room_id}/coding-tasks", json=command).json() == (
            created.json()
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = client.get(f"/api/v1/tasks/{task_id}").json()["state"]
            if state in {"completed", "failed", "needs_human", "cancelled"}:
                break
            time.sleep(0.02)
        assert state == "completed"
        delivery = client.get(f"/api/v1/tasks/{task_id}/delivery")
        assert delivery.status_code == 200, delivery.text
        assert delivery.json()["verification"]["passed"] is True
        assert delivery.json()["completion"]["passed"] is True
        patch_id = delivery.json()["patch"]["artifact_id"]
        assert b"value = 2" in client.get(
            f"/api/v1/tasks/{task_id}/delivery/patch/{patch_id}"
        ).content
        assert (repository / "src/app.py").read_text() == "value = 1\n"


def test_demo_cli_needs_no_keys_and_cleans_temporary_run(monkeypatch) -> None:
    monkeypatch.delenv("KIMI_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    captured = []

    def fake_run(app, **kwargs):
        repository = Path(app.state.chat_coding_service.repository_bound)
        assert repository.exists()
        captured.append((repository.parent, kwargs))

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    assert cli.main(["demo-serve", "--port", "8768"]) == 0
    assert captured[0][1] == {"host": "127.0.0.1", "port": 8768, "workers": 1}
    assert not captured[0][0].exists()
