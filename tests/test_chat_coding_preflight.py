"""P3.1 previews a Human-selected coding scope without granting write authority."""

import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.chat.coding_intent import AuthorizeChatCodingTaskRequest
from app.chat.models import StandaloneChatMessage
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.main import create_app
from app.storage import SQLiteDatabase


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repository), *args), check=True,
        capture_output=True, text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def setup(tmp_path: Path):
    repository = tmp_path / "example"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    (repository / "src").mkdir()
    (repository / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "-c", "user.name=CodeCrew Test", "-c",
         "user.email=test@codecrew.invalid", "commit", "-m", "fixture")
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "chat.sqlite3"))
    store.initialize()
    client = TestClient(create_app(chat_service=StandaloneChatService(store)))
    room = client.post("/api/v1/chats", json={
        "title": "修复 value", "idempotency_key": str(uuid4()),
    }).json()
    room_id = room["room_id"]
    message = client.post(f"/api/v1/chats/{room_id}/messages", json={
        "content": "@白金 先讨论 value 的目标", "idempotency_key": str(uuid4()),
    }).json()["message"]["message"]
    draft = {
        "source_message_id": message["message_id"],
        "repository_path": str(repository),
        "issue": "只修改 src/app.py，把 value 改为 2；运行测试。",
        "allowed_paths": ["src"],
    }
    return client, store, repository, room, draft


def _url(room_id: str) -> str:
    return f"/api/v1/chats/{room_id}/coding-task-preflight"


def test_preflight_returns_git_snapshot_without_task_or_worktree(setup, tmp_path: Path) -> None:
    client, store, repository, room, draft = setup
    response = client.post(_url(room["room_id"]), json=draft)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview == {
        "room_id": room["room_id"],
        "trace_id": room["trace_id"],
        "source_message_id": draft["source_message_id"],
        "issue": draft["issue"],
        "repository_path": str(repository.resolve()),
        "base_commit": _git(repository, "rev-parse", "HEAD"),
        "allowed_paths": ["src"],
        "execution_authorized": False,
        "task_created": False,
    }
    assert client.post(_url(room["room_id"]), json=draft).json() == preview
    assert _git(repository, "status", "--porcelain") == ""
    assert client.get("/api/v1/tasks").status_code == 503
    with store.database.connect() as connection:
        tables = {row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
    assert "tasks" not in tables
    assert not (tmp_path / "worktrees").exists()


def test_preflight_requires_human_source_in_same_active_room(setup) -> None:
    client, store, _repository, room, draft = setup
    other = client.post("/api/v1/chats", json={
        "title": "另一个房间", "idempotency_key": str(uuid4()),
    }).json()
    assert client.post(_url(other["room_id"]), json=draft).status_code == 404
    assert client.post(_url(room["room_id"]), json={
        **draft, "source_message_id": str(uuid4()),
    }).status_code == 404
    planner = next(member for member in room["members"] if member["role"] == "planner")
    human = next(member for member in room["members"] if member["role"] == "human")
    agent = store.append_message(StandaloneChatMessage(
        room_id=UUID(room["room_id"]), trace_id=UUID(room["trace_id"]),
        sender_id=UUID(planner["member_id"]), recipient_ids=(UUID(human["member_id"]),),
        content="Agent 提议编码", idempotency_key="agent-proposal",
    )).message
    assert client.post(_url(room["room_id"]), json={
        **draft, "source_message_id": str(agent.message_id),
    }).status_code == 422
    store.close_room(UUID(room["room_id"]))
    assert client.post(_url(room["room_id"]), json=draft).status_code == 409


def test_preflight_rejects_unsafe_repository_and_write_roots(setup, tmp_path: Path) -> None:
    client, _store, repository, room, draft = setup
    url = _url(room["room_id"])
    for roots in (["."], ["../outside"], ["/tmp"], [".git"], ["src/.env"],
                  ["src", "src"]):
        assert client.post(url, json={**draft, "allowed_paths": roots}).status_code == 422
    assert client.post(url, json={
        **draft, "repository_path": str(repository / "src"),
    }).status_code == 422
    assert client.post(url, json={
        **draft, "repository_path": str(tmp_path / "not-git"),
    }).status_code == 422
    (repository / "new.txt").write_text("untracked\n", encoding="utf-8")
    assert client.post(url, json=draft).status_code == 409


def test_preflight_rejects_symlink_escape(setup, tmp_path: Path) -> None:
    client, _store, repository, room, draft = setup
    outside = tmp_path / "outside"
    outside.mkdir()
    (repository / "linked").symlink_to(outside, target_is_directory=True)
    _git(repository, "add", "linked")
    _git(repository, "-c", "user.name=CodeCrew Test", "-c",
         "user.email=test@codecrew.invalid", "commit", "-m", "linked fixture")
    response = client.post(_url(room["room_id"]), json={
        **draft, "allowed_paths": ["linked"],
    })
    assert response.status_code == 422


def test_future_authorization_command_requires_exact_confirmation(setup) -> None:
    _client, _store, repository, _room, draft = setup
    command = {
        **draft, "expected_base_commit": _git(repository, "rev-parse", "HEAD"),
        "idempotency_key": str(uuid4()),
    }
    with pytest.raises(ValidationError):
        AuthorizeChatCodingTaskRequest.model_validate(command)
    with pytest.raises(ValidationError):
        AuthorizeChatCodingTaskRequest.model_validate({
            **command, "confirmation": "yes",
        })
    accepted = AuthorizeChatCodingTaskRequest.model_validate({
        **command, "confirmation": "authorize_one_coding_task",
    })
    assert accepted.allowed_paths == ("src",)


def test_plain_chat_message_cannot_carry_coding_authorization(setup) -> None:
    client, _store, _repository, room, draft = setup
    response = client.post(f"/api/v1/chats/{room['room_id']}/messages", json={
        "content": "@白金 请讨论能否修改", "idempotency_key": str(uuid4()),
        "repository_path": draft["repository_path"],
        "confirmation": "authorize_one_coding_task",
    })
    assert response.status_code == 422
