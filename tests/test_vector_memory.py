"""Cloud-vector memory is opt-in, scoped, versioned, and lexical-fail-open."""

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.chat.memory import AgentMemoryProposal
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.chat.vector_memory import (
    EmbeddingEndpoint,
    EmbeddingError,
    MemoryVectorIndex,
    OpenAICompatibleEmbedder,
)
from app.main import create_app
from app.storage import SQLiteDatabase


class FakeEmbedder:
    def __init__(self, vectors: dict[str, tuple[float, ...]], fingerprint: str = "model-v1"):
        self.vectors = vectors
        self.model_fingerprint = fingerprint
        self.calls: list[tuple[str, ...]] = []
        self.fail = False

    def embed(self, texts):
        self.calls.append(tuple(texts))
        if self.fail:
            raise EmbeddingError("offline simulated provider failure")
        return tuple(self.vectors[text] for text in texts)


def _setup(tmp_path: Path, embedder: FakeEmbedder, *, min_similarity: float = 0.65):
    database = SQLiteDatabase(tmp_path / "chat.sqlite3")
    vector = MemoryVectorIndex(database, embedder, min_similarity=min_similarity)
    store = StandaloneChatStore(database, vector_index=vector)
    store.initialize()
    client = TestClient(create_app(chat_service=StandaloneChatService(store)))
    return client, store, embedder


def _room(client: TestClient, title: str) -> dict:
    response = client.post("/api/v1/chats", json={
        "title": title, "idempotency_key": str(uuid4()),
    })
    assert response.status_code == 201
    return response.json()


def _save(client: TestClient, room: dict, source_text: str, memory_text: str) -> dict:
    room_id = room["room_id"]
    response = client.post(f"/api/v1/chats/{room_id}/messages", json={
        "content": source_text, "idempotency_key": str(uuid4()),
    })
    assert response.status_code == 201
    source_id = response.json()["message"]["message"]["message_id"]
    saved = client.post(f"/api/v1/chats/{room_id}/memories", json={
        "source_message_id": source_id, "content": memory_text,
    })
    assert saved.status_code == 201
    return saved.json()


def test_semantic_only_recall_and_room_boundary(tmp_path: Path) -> None:
    embedder = FakeEmbedder({
        "默认用中文回答": (1.0, 0.0),
        "首选交流语言是什么": (0.99, 0.01),
        "我偏好清淡饮食": (0.0, 1.0),
    })
    client, store, _ = _setup(tmp_path, embedder)
    first = _room(client, "A")
    second = _room(client, "B")
    saved = _save(client, first, "@白金 以后默认用中文回答", "默认用中文回答")
    _save(client, second, "@白金 我偏好清淡饮食", "我偏好清淡饮食")
    assert [memory.memory_id for memory in store.memory.retrieve(
        UUID(first["room_id"]), "首选交流语言是什么",
    )] == [UUID(saved["memory_id"])]
    assert store.memory.retrieve(UUID(second["room_id"]), "首选交流语言是什么") == ()


def test_same_discussion_exclusion_and_retirement_remove_vector(tmp_path: Path) -> None:
    embedder = FakeEmbedder({
        "默认用中文回答": (1.0, 0.0), "首选交流语言是什么": (1.0, 0.0),
    })
    client, store, _ = _setup(tmp_path, embedder)
    room = _room(client, "A")
    saved = _save(client, room, "@白金 以后默认用中文回答", "默认用中文回答")
    source = store.get_message(UUID(saved["source_message_id"]))
    assert store.memory.retrieve(
        UUID(room["room_id"]), "首选交流语言是什么",
        exclude_correlation_id=source.message.correlation_id,
    ) == ()
    assert client.delete(
        f"/api/v1/chats/{room['room_id']}/memories/{saved['memory_id']}"
    ).status_code == 200
    with store.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM standalone_chat_memory_embeddings WHERE memory_id = ?",
            (saved["memory_id"],),
        ).fetchone()[0] == 0


def test_closed_open_loop_cannot_return_from_vector_index(tmp_path: Path) -> None:
    embedder = FakeEmbedder({
        "下次发布前检查文档链接": (1.0, 0.0),
        "发布前有哪些链接任务": (1.0, 0.0),
    })
    client, store, _ = _setup(tmp_path, embedder)
    room = _room(client, "A")
    room_id = UUID(room["room_id"])
    source = client.post(f"/api/v1/chats/{room_id}/messages", json={
        "content": "@白金 下次发布前检查文档链接",
        "idempotency_key": str(uuid4()),
    }).json()["message"]["message"]
    memory = store.memory.capture_agent_proposal(
        room_id, UUID(source["message_id"]),
        AgentMemoryProposal(kind="open_loop", content="下次发布前检查文档链接"),
    )
    assert memory is not None
    assert store.memory.retrieve(room_id, "发布前有哪些链接任务") == (memory,)
    assert client.post(
        f"/api/v1/chats/{room_id}/memories/{memory.memory_id}/complete",
    ).status_code == 200
    assert store.memory.retrieve(room_id, "发布前有哪些链接任务") == ()
    with store.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM standalone_chat_memory_embeddings WHERE memory_id = ?",
            (str(memory.memory_id),),
        ).fetchone()[0] == 0


def test_embedding_failure_falls_back_and_does_not_undo_memory(tmp_path: Path) -> None:
    embedder = FakeEmbedder({"PR 标题带类型前缀": (1.0, 0.0)})
    embedder.fail = True
    client, store, _ = _setup(tmp_path, embedder)
    room = _room(client, "A")
    saved = _save(client, room, "@白金 PR 标题带类型前缀", "PR 标题带类型前缀")
    assert [memory.memory_id for memory in store.memory.retrieve(
        UUID(room["room_id"]), "PR 标题类型前缀",
    )] == [UUID(saved["memory_id"])]
    assert len(store.memory.list_active(UUID(room["room_id"]))) == 1


def test_model_fingerprint_and_dimensions_prevent_mixed_vectors(tmp_path: Path) -> None:
    embedder = FakeEmbedder({
        "默认用中文回答": (1.0, 0.0), "首选交流语言是什么": (1.0, 0.0),
    })
    client, store, _ = _setup(tmp_path, embedder)
    room = _room(client, "A")
    _save(client, room, "@白金 以后默认用中文回答", "默认用中文回答")
    embedder.vectors["首选交流语言是什么"] = (1.0, 0.0, 0.0)
    assert store.memory.retrieve(UUID(room["room_id"]), "首选交流语言是什么") == ()
    embedder.model_fingerprint = "model-v2"
    embedder.vectors["默认用中文回答"] = (1.0, 0.0, 0.0)
    assert len(store.memory.retrieve(UUID(room["room_id"]), "首选交流语言是什么")) == 1
    with store.database.connect() as connection:
        row = connection.execute(
            "SELECT model_fingerprint, dimensions FROM standalone_chat_memory_embeddings"
        ).fetchone()
    assert (row["model_fingerprint"], row["dimensions"]) == ("model-v2", 3)


def test_duplicate_save_does_not_reembed_or_send_secret_query(tmp_path: Path) -> None:
    embedder = FakeEmbedder({"PR 标题带类型前缀": (1.0, 0.0)})
    client, store, _ = _setup(tmp_path, embedder)
    room = _room(client, "A")
    saved = _save(client, room, "@白金 PR 标题带类型前缀", "PR 标题带类型前缀")
    again = client.post(f"/api/v1/chats/{room['room_id']}/memories", json={
        "source_message_id": saved["source_message_id"],
        "content": "PR 标题带类型前缀",
    })
    assert again.status_code == 201
    assert embedder.calls == [("PR 标题带类型前缀",)]
    store.memory.retrieve(UUID(room["room_id"]), "password 与 PR 标题类型前缀")
    assert embedder.calls == [("PR 标题带类型前缀",)]


def test_openai_compatible_client_checks_response_and_hides_key(monkeypatch) -> None:
    endpoint = EmbeddingEndpoint(
        base_url="https://example.test/v1", model="example-embed",
        revision="2026-10", api_key="test-secret-key",
    )
    reported_tokens = []
    client = OpenAICompatibleEmbedder(endpoint, on_usage_tokens=reported_tokens.append)
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps({"data": [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ], "usage": {"total_tokens": 23}}).encode()

    def fake_urlopen(request, *, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr("app.chat.vector_memory.urllib.request.urlopen", fake_urlopen)
    assert client.embed(["甲", "乙"]) == ((1.0, 0.0), (0.0, 1.0))
    assert reported_tokens == [23]
    request, timeout = requests[0]
    assert request.full_url == "https://example.test/v1/embeddings"
    assert json.loads(request.data)["input"] == ["甲", "乙"]
    assert request.get_header("Authorization") == "Bearer test-secret-key"
    assert timeout == 5.0
    assert "test-secret-key" not in repr(endpoint)


@pytest.mark.parametrize("url", ["http://example.test/v1", "https://example.test/v1/embeddings"])
def test_embedding_endpoint_rejects_unsafe_url(url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS API root"):
        EmbeddingEndpoint(url, "model", "revision", "key")
