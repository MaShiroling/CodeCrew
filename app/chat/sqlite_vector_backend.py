"""Exact reference adapter for the new vector storage contract.

Uses existing schema 24; not selected by CLI yet. Existing MemoryVectorIndex
remains the unchanged production baseline until orchestration is wired later.
"""

import hashlib
import math
import sqlite3
import struct
from collections.abc import Sequence
from uuid import UUID

from app.chat.vector_contracts import (
    VectorAnchor,
    VectorBackendError,
    VectorHit,
    VectorRecord,
    VectorSearch,
)
from app.chat.vector_memory import _cosine
from app.storage import SQLiteDatabase

_CANONICAL = (
    "SELECT m.memory_id, m.room_id, m.source_message_id, m.content, m.status, "
    "s.correlation_id FROM standalone_chat_memories m JOIN standalone_chat_messages s "
    "ON s.message_id=m.source_message_id WHERE m.memory_id=? AND m.room_id=? AND m.status='active'"
)


def _matches(row, anchor):
    return (row is not None and row["source_message_id"] == str(anchor.source_message_id)
            and row["correlation_id"] == str(anchor.source_correlation_id)
            and hashlib.sha256(row["content"].encode("utf-8")).hexdigest() == anchor.content_hash)


class SQLiteMemoryVectorBackend:
    def __init__(self, database: SQLiteDatabase):
        self.database = database

    def upsert(self, records: Sequence[VectorRecord]) -> tuple[UUID, ...]:
        records = tuple(records)
        if (len(records) > 32 or any(not isinstance(item, VectorRecord) for item in records)
                or len({item.anchor.memory_id for item in records}) != len(records)):
            raise ValueError("upsert requires at most 32 distinct typed records")
        if not records:
            return ()
        accepted = []
        try:
            with self.database.transaction() as connection:
                for item in records:
                    anchor = item.anchor
                    canonical = connection.execute(_CANONICAL, (
                        str(anchor.memory_id), str(anchor.room_id),
                    )).fetchone()
                    if not _matches(canonical, anchor):
                        continue
                    dimension = connection.execute(
                        "SELECT dimensions FROM standalone_chat_embedding_meta WHERE model_fingerprint=?",
                        (item.model.fingerprint,),
                    ).fetchone()
                    if dimension is not None and dimension["dimensions"] != item.model.dimensions:
                        raise VectorBackendError("model dimensions changed without revision")
                    connection.execute(
                        "INSERT INTO standalone_chat_embedding_meta(model_fingerprint, dimensions) "
                        "VALUES (?, ?) ON CONFLICT(model_fingerprint) DO NOTHING",
                        (item.model.fingerprint, item.model.dimensions),
                    )
                    connection.execute(
                        "INSERT INTO standalone_chat_memory_embeddings "
                        "(memory_id, model_fingerprint, content_hash, dimensions, vector) VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(memory_id) DO UPDATE SET model_fingerprint=excluded.model_fingerprint, "
                        "content_hash=excluded.content_hash, dimensions=excluded.dimensions, vector=excluded.vector",
                        (str(anchor.memory_id), item.model.fingerprint, anchor.content_hash,
                         item.model.dimensions, struct.pack(f"<{item.model.dimensions}f", *item.vector)),
                    )
                    accepted.append(anchor.memory_id)
        except (sqlite3.Error, OverflowError, struct.error) as exc:
            raise VectorBackendError("SQLite vector write failed") from exc
        return tuple(accepted)

    def delete(self, room_id: UUID, memory_ids: Sequence[UUID]) -> None:
        memory_ids = tuple(memory_ids)
        if (not isinstance(room_id, UUID) or len(memory_ids) > 200
                or any(not isinstance(item, UUID) for item in memory_ids)):
            raise ValueError("delete requires room UUID and at most 200 memory UUIDs")
        if not memory_ids:
            return
        placeholders = ",".join("?" for _ in memory_ids)
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    "DELETE FROM standalone_chat_memory_embeddings WHERE memory_id IN "
                    "(SELECT memory_id FROM standalone_chat_memories WHERE room_id=? AND memory_id IN ("
                    + placeholders + "))", (str(room_id), *(str(item) for item in memory_ids)),
                )
        except sqlite3.Error as exc:
            raise VectorBackendError("SQLite vector delete failed") from exc

    def search(self, request: VectorSearch) -> tuple[VectorHit, ...]:
        if not isinstance(request, VectorSearch):
            raise TypeError("typed vector search required")
        if not request.candidates:
            return ()
        anchors = {str(item.memory_id): item for item in request.candidates}
        placeholders = ",".join("?" for _ in anchors)
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN")
                dimension = connection.execute(
                    "SELECT dimensions FROM standalone_chat_embedding_meta WHERE model_fingerprint=?",
                    (request.model.fingerprint,),
                ).fetchone()
                if dimension is not None and dimension["dimensions"] != request.model.dimensions:
                    raise VectorBackendError("query dimensions differ from stored model")
                rows = connection.execute(
                    "SELECT m.memory_id, m.content, m.source_message_id, s.correlation_id, "
                    "e.content_hash, e.vector FROM standalone_chat_memory_embeddings e "
                    "JOIN standalone_chat_memories m ON m.memory_id=e.memory_id "
                    "JOIN standalone_chat_messages s ON s.message_id=m.source_message_id "
                    "WHERE m.room_id=? AND m.status='active' "
                    "AND (? IS NULL OR s.correlation_id != ?) "
                    "AND e.model_fingerprint=? AND e.dimensions=? AND m.memory_id IN (" + placeholders + ")",
                    (str(request.room_id), str(request.exclude_correlation_id)
                     if request.exclude_correlation_id else None,
                     str(request.exclude_correlation_id) if request.exclude_correlation_id else None,
                     request.model.fingerprint, request.model.dimensions, *anchors),
                ).fetchall()
        except sqlite3.Error as exc:
            raise VectorBackendError("SQLite vector search failed") from exc
        ranked = []
        for row in rows:
            anchor: VectorAnchor = anchors[row["memory_id"]]
            if (not _matches(row, anchor) or row["content_hash"] != anchor.content_hash
                    or not isinstance(row["vector"], bytes)
                    or len(row["vector"]) != 4 * request.model.dimensions):
                continue
            vector = struct.unpack(f"<{request.model.dimensions}f", row["vector"])
            if not all(math.isfinite(value) for value in vector):
                continue
            similarity = _cosine(request.vector, vector)
            # Preserve the existing zero-norm score (-1); nonfinite blobs are invalid.
            similarity = max(-1.0, min(1.0, similarity))
            if similarity >= request.min_similarity:
                ranked.append(VectorHit(anchor, request.model, similarity))
        ranked.sort(key=lambda item: (-item.similarity, str(item.anchor.memory_id)))
        return tuple(ranked[:request.limit])
