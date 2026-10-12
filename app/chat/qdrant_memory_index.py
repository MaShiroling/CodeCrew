"""Opt-in query orchestration over the versioned outbox namespace.

SQLite facts/cache/revisions remain authoritative. Indexing writes the local
cache only; search never initializes, subscribes, delivers or backfills. Remote
delivery is explicit, and read failures are handled by the lexical caller.
"""

import hashlib
from uuid import UUID

from app.chat.memory import _SENSITIVE
from app.chat.qdrant_vector_sync import QdrantVersionedVectorBackend
from app.chat.sqlite_vector_backend import SQLiteMemoryVectorBackend
from app.chat.vector_contracts import (
    VectorAnchor,
    VectorBackendError,
    VectorHit,
    VectorModel,
    VectorRecord,
    VectorSearch,
)
from app.chat.vector_outbox import MemoryVectorOutbox

SNAPSHOT_READ_REVISION = "codecrew-canonical-batch-v1"


class QdrantMemoryVectorIndex:
    def __init__(self, database, embedder, endpoint, *, min_similarity=0.65, backend_factory=None):
        if type(min_similarity) not in (int, float) or not -1 <= min_similarity <= 1:
            raise ValueError("invalid vector similarity threshold")
        self.database, self.embedder = database, embedder
        self.min_similarity = min_similarity
        self._backend = backend_factory or (lambda model: QdrantVersionedVectorBackend(endpoint, model))

    @staticmethod
    def _inputs(memories, limit):
        memories = tuple(memories)
        if (len(memories) > limit or any(not isinstance(item.memory_id, UUID) for item in memories)
                or len({item.memory_id for item in memories}) != len(memories)):
            raise ValueError("bounded distinct canonical memories required")
        return memories

    def _snapshot(self, conn, memories, *, cached_only, room_id=None, exclude=None):
        memories = self._inputs(memories, 200)
        conn.execute("BEGIN")
        meta = conn.execute("SELECT dimensions FROM standalone_chat_embedding_meta WHERE model_fingerprint=?",
                            (self.embedder.model_fingerprint,)).fetchone()
        if meta is None and cached_only:
            return None, ()
        # 2 is eligibility-only before the provider establishes actual dimensions.
        # It is never used for remote search or persisted as a model declaration.
        model = VectorModel(self.embedder.model_fingerprint, meta[0] if meta else 2)
        backend = self._backend(model)
        if backend.model != model:
            raise VectorBackendError("vector backend model differs from canonical cache")
        worker = MemoryVectorOutbox(self.database, backend)
        if cached_only:
            worker._require_target(conn)  # Explicit operator preparation, never auto-register.
        rows = {}
        if memories:
            placeholders = ",".join("?" for _ in memories)
            rows = {row["memory_id"]: row for row in conn.execute(
                "SELECT r.*,m.content,m.source_message_id,s.correlation_id "
                "FROM standalone_chat_vector_revisions r JOIN standalone_chat_memories m "
                "ON m.memory_id=r.memory_id JOIN standalone_chat_messages s "
                "ON s.message_id=m.source_message_id "
                f"WHERE r.memory_id IN ({placeholders})", tuple(str(m.memory_id) for m in memories))}
        eligible = []
        for memory in memories:
            row = rows.get(str(memory.memory_id))
            if (row is None or row["content"] != memory.content
                    or (room_id is not None and row["room_id"] != str(room_id))
                    or (exclude is not None and row["correlation_id"] == str(exclude))
                    or str(getattr(memory, "room_id", row["room_id"])) != row["room_id"]
                    or str(getattr(memory, "source_message_id", row["source_message_id"])) != row["source_message_id"]):
                continue
            eligible.append((memory, row))
        desired = worker.desired_many_in_snapshot(conn, tuple(row for _, row in eligible))
        selected = []
        for memory, row in eligible:
            mutation, waiting = desired[row["memory_id"]]
            if mutation.record is None and (cached_only or waiting is None):
                continue  # No cache, retired, external or sensitive evidence.
            anchor = VectorAnchor(memory.memory_id, UUID(row["room_id"]), UUID(row["source_message_id"]),
                                  UUID(row["correlation_id"]), hashlib.sha256(memory.content.encode()).hexdigest())
            selected.append((memory, mutation, anchor))
        return worker, tuple(selected)

    def index(self, memories):
        memories = self._inputs(memories, 32)
        if not memories:
            return
        with self.database.connect() as conn:
            _, selected = self._snapshot(conn, memories, cached_only=False)
        needed = tuple(item for item in selected if item[1].record is None)
        if not needed:
            return
        vectors = self.embedder.embed([memory.content for memory, _, _ in needed])
        if len(vectors) != len(needed) or len({len(vector) for vector in vectors}) != 1:
            raise VectorBackendError("embedding batch dimensions or count invalid")
        model = VectorModel(self.embedder.model_fingerprint, len(vectors[0]))
        # Recheck after cloud work. The cache adapter rechecks anchors again in its
        # write transaction; subsequent source changes are fenced by the outbox.
        with self.database.connect() as conn:
            _, current = self._snapshot(conn, memories, cached_only=False)
        current = {mutation.memory_id: mutation for _, mutation, _ in current}
        records = tuple(VectorRecord(anchor, model, vector) for (_, mutation, anchor), vector
                        in zip(needed, vectors, strict=True) if current.get(mutation.memory_id) == mutation)
        SQLiteMemoryVectorBackend(self.database).upsert(records)

    def search(self, query, memories):
        return self.search_scoped(query, memories)

    def search_scoped(self, query, memories, *, room_id=None, exclude_correlation_id=None):
        if ((room_id is not None and not isinstance(room_id, UUID))
                or (exclude_correlation_id is not None and not isinstance(exclude_correlation_id, UUID))):
            raise ValueError("typed recall scope IDs required")
        memories = self._inputs(memories, 200)
        if not memories or not query.strip() or _SENSITIVE.search(query):
            return ()
        with self.database.connect() as conn:
            worker, selected = self._snapshot(conn, memories, cached_only=True,
                                             room_id=room_id, exclude=exclude_correlation_id)
        if not selected:
            return ()  # No query embedding for empty/untrusted/missing-cache candidates.
        mutations = tuple(mutation for _, mutation, _ in selected)
        anchors = tuple(anchor for _, _, anchor in selected)
        if len({anchor.room_id for anchor in anchors}) != 1:
            raise ValueError("vector recall requires one room")
        backend = worker.backend
        backend._require_sync_ready()  # Fail cheaply before a paid query embedding.
        vectors = self.embedder.embed([query])
        if len(vectors) != 1:
            raise VectorBackendError("query embedding count invalid")
        vector = vectors[0]
        request = VectorSearch(anchors[0].room_id, backend.model, vector, anchors,
                               exclude_correlation_id=exclude_correlation_id,
                               limit=len(anchors), min_similarity=self.min_similarity)
        hits = backend.search_current(request, mutations)
        expected = {mutation.memory_id: mutation for mutation in mutations}
        if (not isinstance(hits, tuple) or len(hits) > len(expected)
                or any(not isinstance(hit, VectorHit) or hit.model != request.model
                       or hit.anchor.memory_id not in expected
                       or hit.anchor != expected[hit.anchor.memory_id].record.anchor
                       or hit.similarity < request.min_similarity for hit in hits)
                or len({hit.anchor.memory_id for hit in hits}) != len(hits)):
            raise VectorBackendError("invalid canonical-scoped vector hits")
        with self.database.connect() as conn:
            _, current = self._snapshot(conn, memories, cached_only=True,
                                        room_id=request.room_id, exclude=exclude_correlation_id)
        current = {mutation.memory_id: mutation for _, mutation, _ in current}
        hits = sorted(hits, key=lambda hit: (-hit.similarity, str(hit.anchor.memory_id)))
        return tuple((hit.anchor.memory_id, hit.similarity) for hit in hits
                     if current.get(hit.anchor.memory_id) == expected[hit.anchor.memory_id])
