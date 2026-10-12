"""Provider-neutral vector ports; no embedding, network, config or migrations.

SQLite canonical memories remain authoritative. Search is candidate-bounded in
this phase: external ANN must not silently widen the existing 200-ID boundary.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol
from uuid import UUID


def _uuid(value):
    if not isinstance(value, UUID):
        raise TypeError("vector scope IDs must be UUIDs")


def _vector(value, dimensions):
    if (not isinstance(value, tuple) or len(value) != dimensions
            or any(type(x) not in (int, float) or abs(x) > 3.4028234663852886e38
                   or not math.isfinite(x) for x in value)):
        raise ValueError("vector must be a finite immutable tuple of model dimensions")


@dataclass(frozen=True, slots=True)
class VectorModel:
    fingerprint: str
    dimensions: int
    metric: Literal["cosine"] = "cosine"

    def __post_init__(self):
        if (not isinstance(self.fingerprint, str) or not self.fingerprint.strip()
                or len(self.fingerprint) > 128):
            raise ValueError("model fingerprint is required")
        if type(self.dimensions) is not int or not 2 <= self.dimensions <= 4096:
            raise ValueError("model dimensions must be within 2–4096")
        if self.metric != "cosine":
            raise ValueError("only cosine similarity is supported")


@dataclass(frozen=True, slots=True)
class VectorAnchor:
    """Metadata from a canonical read, never guessed from model output."""

    memory_id: UUID
    room_id: UUID
    source_message_id: UUID
    source_correlation_id: UUID
    content_hash: str
    status: Literal["active"] = "active"

    def __post_init__(self):
        for value in (self.memory_id, self.room_id, self.source_message_id, self.source_correlation_id):
            _uuid(value)
        if (not isinstance(self.content_hash, str) or len(self.content_hash) != 64
                or any(x not in "0123456789abcdef" for x in self.content_hash)):
            raise ValueError("content hash must be lowercase SHA-256")
        if self.status != "active":
            raise ValueError("only active memories may enter the vector index")


@dataclass(frozen=True, slots=True)
class VectorRecord:
    anchor: VectorAnchor
    model: VectorModel
    vector: tuple[float, ...] = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.anchor, VectorAnchor) or not isinstance(self.model, VectorModel):
            raise TypeError("typed vector anchor and model required")
        _vector(self.vector, self.model.dimensions)


@dataclass(frozen=True, slots=True)
class VectorSearch:
    room_id: UUID
    model: VectorModel
    vector: tuple[float, ...] = field(repr=False)
    candidates: tuple[VectorAnchor, ...]
    exclude_correlation_id: UUID | None = None
    limit: int = 200
    min_similarity: float = 0.65

    def __post_init__(self):
        _uuid(self.room_id)
        if self.exclude_correlation_id is not None:
            _uuid(self.exclude_correlation_id)
        if not isinstance(self.model, VectorModel):
            raise TypeError("typed vector model required")
        _vector(self.vector, self.model.dimensions)
        if (not isinstance(self.candidates, tuple) or len(self.candidates) > 200
                or any(not isinstance(item, VectorAnchor) for item in self.candidates)):
            raise ValueError("at most 200 immutable candidate anchors required")
        if len({item.memory_id for item in self.candidates}) != len(self.candidates):
            raise ValueError("duplicate vector candidate IDs")
        if any(item.room_id != self.room_id or (
                self.exclude_correlation_id is not None
                and item.source_correlation_id == self.exclude_correlation_id
        ) for item in self.candidates):
            raise ValueError("candidate outside room/discussion scope")
        if type(self.limit) is not int or not 1 <= self.limit <= 200:
            raise ValueError("vector search limit must be within 1–200")
        if (type(self.min_similarity) not in (int, float)
                or not math.isfinite(self.min_similarity) or not -1 <= self.min_similarity <= 1):
            raise ValueError("invalid cosine threshold")


@dataclass(frozen=True, slots=True)
class VectorHit:
    anchor: VectorAnchor
    model: VectorModel
    similarity: float

    def __post_init__(self):
        if not isinstance(self.anchor, VectorAnchor) or not isinstance(self.model, VectorModel):
            raise TypeError("typed vector anchor and model required")
        if (type(self.similarity) not in (int, float)
                or not math.isfinite(self.similarity) or not -1 <= self.similarity <= 1):
            raise ValueError("invalid cosine similarity")


class VectorBackendError(RuntimeError):
    """Safe category only; do not include provider bodies/URLs/credentials."""


class MemoryVectorBackend(Protocol):
    """Derived storage only; no calls to embedding models or canonical mutations.

    Upsert returns accepted IDs and is idempotent for identical records. Callers
    must validate canonical anchors before sending; the local reference also
    rejects stale anchors in its write transaction. Delete is room-scoped/idempotent. Search must filter
    room/active/discussion/model/dimension/allowed anchors BEFORE ranking/limit;
    results use cosine descending then memory UUID ascending. No payload text.
    External adapters must reject unsupported filtering rather than widen scope.
    Empty result is success; backend unavailable raises VectorBackendError.
    Ordered-event revision/tombstone semantics use the separate
    VersionedVectorBackend port; this legacy port alone provides no fencing.
    """

    def upsert(self, records: Sequence[VectorRecord]) -> tuple[UUID, ...]: ...

    def delete(self, room_id: UUID, memory_ids: Sequence[UUID]) -> None: ...

    def search(self, request: VectorSearch) -> tuple[VectorHit, ...]: ...


class IndexMemory(Protocol):
    memory_id: UUID
    content: str


class MemoryVectorIndexPort(Protocol):
    """Existing embedding/index orchestration boundary, unchanged calling shape."""

    def index(self, memories: Sequence[IndexMemory]) -> None: ...

    def search(self, query: str, memories: Sequence[IndexMemory]) -> tuple[tuple[UUID, float], ...]: ...


class ScopedMemoryVectorIndexPort(MemoryVectorIndexPort, Protocol):
    """Optional scoped extension; legacy indexes retain their original shape."""

    def search_scoped(self, query: str, memories: Sequence[IndexMemory], *,
                      room_id: UUID | None = None, exclude_correlation_id: UUID | None = None
                      ) -> tuple[tuple[UUID, float], ...]: ...
