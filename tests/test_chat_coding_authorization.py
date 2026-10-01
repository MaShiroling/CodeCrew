"""P3.2: explicit Human grant creates at most one pinned, policy-matched Task."""

import subprocess
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_task_api_e2e import make_repository, make_runtime

from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.main import create_app
from app.storage import SQLiteDatabase
from app.workspace import PermissionPolicy


def _chat_service(tmp_path: Path) -> StandaloneChatService:
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "tasks.sqlite3"))
    store.initialize()
    return StandaloneChatService(store)


def _room_and_draft(client: TestClient, repository: Path) -> tuple[str, dict]:
    room = client.post("/api/v1/chats", json={
        "title": "授权一次 value 修改", "idempotency_key": str(uuid4()),
    }).json()
    room_id = room["room_id"]
    message = client.post(f"/api/v1/chats/{room_id}/messages", json={
        "content": "@白金 讨论将 value 改为 2", "idempotency_key": str(uuid4()),
    }).json()["message"]["message"]
    draft = {
        "source_message_id": message["message_id"],
        "repository_path": str(repository),
        "issue": "Set value to two",
        "allowed_paths": ["src"],
    }
    return room_id, draft


def test_human_authorizes_one_pinned_task_and_retries_are_idempotent(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    runtime, _agents = make_runtime(tmp_path)
    chat = _chat_service(tmp_path)
    app = create_app(runtime=runtime, chat_service=chat,
                     chat_coding_policy=PermissionPolicy(allowed_paths=("src",)))
    with TestClient(app) as client:
        assert client.get("/api/v1/chats/coding-capability").json() == {
            "available": True, "allowed_paths": ["src"],
        }
        room_id, draft = _room_and_draft(client, repository)
        preview = client.post(f"/api/v1/chats/{room_id}/coding-task-preflight", json=draft)
        assert preview.status_code == 200
        base = preview.json()["base_commit"]
        command = {
            **draft, "idempotency_key": str(uuid4()), "expected_base_commit": base,
            "confirmation": "authorize_one_coding_task",
        }
        url = f"/api/v1/chats/{room_id}/coding-tasks"
        created = client.post(url, json=command)
        assert created.status_code == 201, created.text
        receipt = created.json()
        assert receipt["execution_authorized"] is True
        assert receipt["task_created"] is True
        task_id = UUID(receipt["task_id"])
        assert receipt["base_commit"] == base
        assert receipt["allowed_paths"] == ["src"]
        assert runtime.service.contexts.get(task_id).context.worktree.base_revision == base
        assert client.post(url, json=command).json() == receipt
        assert client.post(url, json={**command, "issue": "different"}).status_code == 409
        assert client.post(url, json={
            **command, "idempotency_key": str(uuid4()),
        }).status_code == 409
        assert len(client.get("/api/v1/tasks").json()["items"]) == 1
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = client.get(f"/api/v1/tasks/{task_id}").json()["state"]
            if state in {"completed", "failed", "cancelled", "needs_human"}:
                break
            time.sleep(0.02)
        else:
            pytest.fail("authorized task did not settle")
        assert state == "completed"
        delivery = client.get(f"/api/v1/tasks/{task_id}/delivery")
        assert delivery.status_code == 200
        assert delivery.json()["delivery_ready"] is True
        assert delivery.json()["verification"]["passed"] is True
        assert delivery.json()["completion"]["passed"] is True
        patch_id = delivery.json()["patch"]["artifact_id"]
        patch = client.get(f"/api/v1/tasks/{task_id}/delivery/patch/{patch_id}")
        assert patch.status_code == 200
        assert b"value = 2" in patch.content


def test_authorization_rejects_scope_or_stale_baseline_without_task(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    runtime, _agents = make_runtime(tmp_path)
    chat = _chat_service(tmp_path)
    app = create_app(runtime=runtime, chat_service=chat,
                     chat_coding_policy=PermissionPolicy(allowed_paths=("src",)))
    with TestClient(app) as client:
        room_id, draft = _room_and_draft(client, repository)
        preview = client.post(f"/api/v1/chats/{room_id}/coding-task-preflight", json=draft)
        base = preview.json()["base_commit"]
        url = f"/api/v1/chats/{room_id}/coding-tasks"
        command = {
            **draft, "idempotency_key": str(uuid4()), "expected_base_commit": base,
            "confirmation": "authorize_one_coding_task",
        }
        assert client.post(url, json={
            **command, "allowed_paths": ["src/app.py"],
        }).status_code == 409
        assert client.post(url, json={
            **command, "expected_base_commit": "0" * 40,
        }).status_code == 409
        assert client.post(url, json={
            **command, "confirmation": "yes",
        }).status_code == 422
        (repository / "src/app.py").write_text("value = 3\n", encoding="utf-8")
        subprocess.run(("git", "add", "src/app.py"), cwd=repository,
                       check=True, capture_output=True)
        subprocess.run(("git", "-c", "user.name=CodeCrew Test", "-c",
                        "user.email=test@codecrew.invalid", "commit", "-m", "new baseline"),
                       cwd=repository, check=True, capture_output=True)
        assert client.post(url, json=command).status_code == 409
        assert client.get("/api/v1/tasks").json()["items"] == []
        assert not (tmp_path / "worktrees").exists()


def test_chat_only_app_keeps_coding_disabled(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    chat = _chat_service(tmp_path)
    with TestClient(create_app(chat_service=chat)) as client:
        assert client.get("/api/v1/chats/coding-capability").json() == {
            "available": False, "allowed_paths": [],
        }
        room_id, draft = _room_and_draft(client, repository)
        command = {
            **draft, "idempotency_key": str(uuid4()), "expected_base_commit": "0" * 40,
            "confirmation": "authorize_one_coding_task",
        }
        assert client.post(f"/api/v1/chats/{room_id}/coding-tasks", json=command).status_code == 503


def test_authorization_cannot_be_mounted_with_a_different_runtime_policy(tmp_path: Path) -> None:
    runtime, _agents = make_runtime(tmp_path)
    chat = _chat_service(tmp_path)
    with pytest.raises(ValueError, match="must match"):
        create_app(runtime=runtime, chat_service=chat,
                   chat_coding_policy=PermissionPolicy(allowed_paths=("other",)))


def test_failed_bootstrap_keeps_pending_claim_and_does_not_retry(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    chat = _chat_service(tmp_path)

    class FailingTaskService:
        def __init__(self):
            self.calls = 0
            self.permission_policy = PermissionPolicy(allowed_paths=("src",))

        async def create_task(self, _request):
            self.calls += 1
            raise RuntimeError("simulated bootstrap failure")

    task_service = FailingTaskService()
    app = create_app(task_service=task_service, chat_service=chat,
                     chat_coding_policy=PermissionPolicy(allowed_paths=("src",)))
    with TestClient(app) as client:
        room_id, draft = _room_and_draft(client, repository)
        base = client.post(f"/api/v1/chats/{room_id}/coding-task-preflight",
                           json=draft).json()["base_commit"]
        command = {
            **draft, "idempotency_key": str(uuid4()), "expected_base_commit": base,
            "confirmation": "authorize_one_coding_task",
        }
        url = f"/api/v1/chats/{room_id}/coding-tasks"
        with pytest.raises(RuntimeError, match="simulated bootstrap failure"):
            client.post(url, json=command)
        assert client.post(url, json=command).status_code == 409
        assert task_service.calls == 1
