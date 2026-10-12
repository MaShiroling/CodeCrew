"""Eval-only rowwise control retained for batch parity and later paired timing.

Uses the current shared outbox validator, not a byte-for-byte historical build.
Never selected by app config. Caller must provide isolated fixtures and an owned
read snapshot, just like QdrantMemoryVectorIndex._snapshot (no remote calls).
"""

import hashlib
from uuid import UUID

from app.chat.vector_contracts import VectorAnchor, VectorBackendError, VectorModel
from app.chat.vector_outbox import MemoryVectorOutbox


def rowwise_snapshot(index, conn, memories, *, cached_only, room_id=None, exclude=None):
    memories = index._inputs(memories, 200)
    conn.execute("BEGIN")
    meta = conn.execute("SELECT dimensions FROM standalone_chat_embedding_meta WHERE model_fingerprint=?",
                        (index.embedder.model_fingerprint,)).fetchone()
    if meta is None and cached_only:
        return None, ()
    model = VectorModel(index.embedder.model_fingerprint, meta[0] if meta else 2)
    backend = index._backend(model)
    if backend.model != model:
        raise VectorBackendError("vector backend model differs from canonical cache")
    worker = MemoryVectorOutbox(index.database, backend)
    if cached_only:
        worker._require_target(conn)
    selected = []
    for memory in memories:
        row = conn.execute("SELECT r.*,m.content,m.source_message_id,s.correlation_id "
                           "FROM standalone_chat_vector_revisions r JOIN standalone_chat_memories m "
                           "ON m.memory_id=r.memory_id JOIN standalone_chat_messages s "
                           "ON s.message_id=m.source_message_id WHERE r.memory_id=?",
                           (str(memory.memory_id),)).fetchone()
        if (row is None or row["content"] != memory.content
                or (room_id is not None and row["room_id"] != str(room_id))
                or (exclude is not None and row["correlation_id"] == str(exclude))
                or str(getattr(memory, "room_id", row["room_id"])) != row["room_id"]
                or str(getattr(memory, "source_message_id", row["source_message_id"])) != row["source_message_id"]):
            continue
        mutation, waiting = worker.desired_in_snapshot(conn, row)
        if mutation.record is None and (cached_only or waiting is None):
            continue
        anchor = VectorAnchor(memory.memory_id, UUID(row["room_id"]), UUID(row["source_message_id"]),
                              UUID(row["correlation_id"]), hashlib.sha256(memory.content.encode()).hexdigest())
        selected.append((memory, mutation, anchor))
    return worker, tuple(selected)
