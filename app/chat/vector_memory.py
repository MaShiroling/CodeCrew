"""Opt-in cloud embeddings and bounded SQLite vector recall for room memories.

The vector table is a derived index; chat memories remain the source of truth.
At 200 active memories per room, an exact in-process cosine scan is simpler
than requiring a loadable SQLite extension on every supported host.
"""

import hashlib
import json
import math
import struct
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from app.storage import SQLiteDatabase


class EmbeddingError(RuntimeError):
    """Embedding failed without exposing provider response bodies or credentials."""


class Embedder(Protocol):
    @property
    def model_fingerprint(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...


class VectorMemory(Protocol):
    memory_id: UUID
    content: str


@dataclass(frozen=True, slots=True)
class EmbeddingEndpoint:
    base_url: str
    model: str
    revision: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path.endswith("/embeddings")):
            raise ValueError("embedding base URL must be an HTTPS API root, not an endpoint")
        if not self.model.strip() or not self.revision.strip() or not self.api_key.strip():
            raise ValueError("embedding model, revision and API key are required")
        if not 0 < self.timeout_seconds <= 30:
            raise ValueError("embedding timeout must be within 0–30 seconds")

    @property
    def model_fingerprint(self) -> str:
        identity = json.dumps(
            [self.base_url.rstrip("/"), self.model, self.revision],
            ensure_ascii=False, separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class OpenAICompatibleEmbedder:
    """Minimal POST /embeddings client; no SDK or provider-specific model assumption."""

    def __init__(
        self, endpoint: EmbeddingEndpoint, *,
        on_usage_tokens: Callable[[int], None] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.on_usage_tokens = on_usage_tokens

    @property
    def model_fingerprint(self) -> str:
        return self.endpoint.model_fingerprint

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not texts or len(texts) > 32 or any(not text.strip() for text in texts):
            raise ValueError("embedding input must contain 1–32 nonempty texts")
        payload = json.dumps({
            "model": self.endpoint.model, "input": list(texts), "encoding_format": "float",
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint.base_url.rstrip("/") + "/embeddings",
            data=payload, method="POST",
            headers={
                "Authorization": f"Bearer {self.endpoint.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.endpoint.timeout_seconds) as response:
                raw = response.read(8_000_001)
        except (OSError, urllib.error.URLError) as exc:
            raise EmbeddingError("embedding provider request failed") from exc
        if len(raw) > 8_000_000:
            raise EmbeddingError("embedding provider response is too large")
        try:
            data = json.loads(raw)
            items = data["data"]
            if not isinstance(items, list) or len(items) != len(texts):
                raise ValueError("unexpected embedding count")
            ordered: list[tuple[float, ...] | None] = [None] * len(texts)
            for item in items:
                index = item["index"]
                if not isinstance(index, int) or not 0 <= index < len(texts) or ordered[index]:
                    raise ValueError("invalid embedding index")
                vector = tuple(float(value) for value in item["embedding"])
                if not 2 <= len(vector) <= 4096 or not all(math.isfinite(x) for x in vector):
                    raise ValueError("invalid embedding vector")
                ordered[index] = vector
            if any(vector is None for vector in ordered):
                raise ValueError("missing embedding")
            vectors = tuple(vector for vector in ordered if vector is not None)
            if len({len(vector) for vector in vectors}) != 1:
                raise ValueError("inconsistent embedding dimensions")
            usage = data.get("usage")
            tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
            if (self.on_usage_tokens is not None and type(tokens) is int
                    and tokens >= 0):
                self.on_usage_tokens(tokens)
            return vectors
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise EmbeddingError("invalid embedding provider response") from exc


class MemoryVectorIndex:
    """Derived vector index with model/content anchors and exact cosine search."""

    def __init__(
        self, database: SQLiteDatabase, embedder: Embedder, *,
        min_similarity: float = 0.65, backfill_limit: int = 10,
    ) -> None:
        if not -1 <= min_similarity <= 1:
            raise ValueError("invalid vector similarity threshold")
        if not 0 <= backfill_limit <= 32:
            raise ValueError("invalid vector backfill limit")
        self.database = database
        self.embedder = embedder
        self.min_similarity = min_similarity
        self.backfill_limit = backfill_limit

    def index(self, memories: Sequence[VectorMemory]) -> None:
        if not memories:
            return
        if len(memories) > 32:
            raise ValueError("embedding batch exceeds 32 memories")
        ids = tuple(str(memory.memory_id) for memory in memories)
        placeholders = ",".join("?" for _ in ids)
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT memory_id, model_fingerprint, content_hash "
                "FROM standalone_chat_memory_embeddings WHERE memory_id IN ("
                + placeholders + ")", ids,
            ).fetchall()
        by_id = {row["memory_id"]: row for row in existing}
        needed = []
        for memory in memories:
            row = by_id.get(str(memory.memory_id))
            if (row is None or row["model_fingerprint"] != self.embedder.model_fingerprint
                    or row["content_hash"] != hashlib.sha256(
                        memory.content.encode("utf-8")
                    ).hexdigest()):
                needed.append(memory)
        if not needed:
            return
        vectors = self.embedder.embed([memory.content for memory in needed])
        now_rows = []
        for memory, vector in zip(needed, vectors, strict=True):
            now_rows.append((
                str(memory.memory_id), self.embedder.model_fingerprint,
                hashlib.sha256(memory.content.encode("utf-8")).hexdigest(),
                len(vector), struct.pack(f"<{len(vector)}f", *vector),
            ))
        with self.database.transaction() as connection:
            dimension = now_rows[0][3]
            if any(row[3] != dimension for row in now_rows):
                raise EmbeddingError("embedding dimensions changed within a batch")
            anchor = connection.execute(
                "SELECT dimensions FROM standalone_chat_embedding_meta "
                "WHERE model_fingerprint = ?", (self.embedder.model_fingerprint,),
            ).fetchone()
            if anchor is not None and anchor["dimensions"] != dimension:
                raise EmbeddingError("embedding dimensions changed without model revision")
            if anchor is None:
                connection.execute(
                    "INSERT INTO standalone_chat_embedding_meta "
                    "(model_fingerprint, dimensions) VALUES (?, ?)",
                    (self.embedder.model_fingerprint, dimension),
                )
            for memory_id, fingerprint, content_hash, dim, packed in now_rows:
                connection.execute(
                    "INSERT INTO standalone_chat_memory_embeddings "
                    "(memory_id, model_fingerprint, content_hash, dimensions, vector) "
                    "SELECT ?, ?, ?, ?, ? WHERE EXISTS ("
                    "SELECT 1 FROM standalone_chat_memories "
                    "WHERE memory_id = ? AND status = 'active') "
                    "ON CONFLICT(memory_id) DO UPDATE SET "
                    "model_fingerprint=excluded.model_fingerprint, "
                    "content_hash=excluded.content_hash, dimensions=excluded.dimensions, "
                    "vector=excluded.vector",
                    (memory_id, fingerprint, content_hash, dim, packed, memory_id),
                )

    def search(
        self, query: str, memories: Sequence[VectorMemory],
    ) -> tuple[tuple[UUID, float], ...]:
        if not memories:
            return ()
        memory_by_id = {str(memory.memory_id): memory for memory in memories}
        placeholders = ",".join("?" for _ in memory_by_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT memory_id, model_fingerprint, content_hash, dimensions, vector "
                "FROM standalone_chat_memory_embeddings WHERE memory_id IN ("
                + placeholders + ")",
                tuple(memory_by_id),
            ).fetchall()
        current = {}
        for row in rows:
            memory = memory_by_id[row["memory_id"]]
            if (row["model_fingerprint"] == self.embedder.model_fingerprint
                    and row["content_hash"] == hashlib.sha256(
                        memory.content.encode("utf-8")
                    ).hexdigest()):
                current[row["memory_id"]] = row
        missing = [memory for key, memory in memory_by_id.items() if key not in current]
        if missing and self.backfill_limit:
            self.index(missing[:self.backfill_limit])
        return self._rank(query, memories)

    def _rank(
        self, query: str, memories: Sequence[VectorMemory],
    ) -> tuple[tuple[UUID, float], ...]:
        by_id = {str(memory.memory_id): memory for memory in memories}
        placeholders = ",".join("?" for _ in by_id)
        with self.database.connect() as connection:
            anchor = connection.execute(
                "SELECT dimensions FROM standalone_chat_embedding_meta "
                "WHERE model_fingerprint = ?", (self.embedder.model_fingerprint,),
            ).fetchone()
            rows = connection.execute(
                "SELECT memory_id, content_hash, dimensions, vector "
                "FROM standalone_chat_memory_embeddings WHERE model_fingerprint = ? "
                "AND memory_id IN (" + placeholders + ")",
                (self.embedder.model_fingerprint, *by_id),
            ).fetchall()
        if not rows:
            return ()
        query_vector = self.embedder.embed([query])[0]
        if anchor is not None and anchor["dimensions"] != len(query_vector):
            raise EmbeddingError("query embedding dimensions changed without model revision")
        ranked = []
        for row in rows:
            memory = by_id[row["memory_id"]]
            if (row["dimensions"] != len(query_vector)
                    or row["content_hash"] != hashlib.sha256(
                        memory.content.encode("utf-8")
                    ).hexdigest()):
                continue
            if len(row["vector"]) != 4 * row["dimensions"]:
                continue
            vector = struct.unpack(f"<{row['dimensions']}f", row["vector"])
            similarity = _cosine(query_vector, vector)
            if similarity >= self.min_similarity:
                ranked.append((UUID(row["memory_id"]), similarity))
        ranked.sort(key=lambda item: (-item[1], str(item[0])))
        return tuple(ranked)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = math.fsum(a * b for a, b in zip(left, right, strict=True))
    norms = math.sqrt(math.fsum(a * a for a in left) * math.fsum(b * b for b in right))
    return dot / norms if norms else -1.0
