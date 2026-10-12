"""Version-fenced derived mutations, separate from the legacy storage port."""

import hashlib
import struct
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.chat.vector_contracts import VectorModel, VectorRecord, _uuid

MAX_SYNC_REVISION = 9_007_199_254_740_991  # Exact integer in JSON/range comparisons.


@dataclass(frozen=True, slots=True)
class VectorMutation:
    room_id: UUID
    memory_id: UUID
    revision: int
    model: VectorModel
    record: VectorRecord | None = None  # None means a persistent, non-searchable tombstone.

    def __post_init__(self):
        _uuid(self.room_id)
        _uuid(self.memory_id)
        if type(self.revision) is not int or not 1 <= self.revision <= MAX_SYNC_REVISION:
            raise ValueError("invalid vector synchronization revision")
        if not isinstance(self.model, VectorModel):
            raise TypeError("typed synchronization model required")
        if self.record is not None and (
            not isinstance(self.record, VectorRecord) or self.record.model != self.model
            or self.record.anchor.room_id != self.room_id
            or self.record.anchor.memory_id != self.memory_id
            or not any(self.record.vector)
        ):
            raise ValueError("mutation record must match scope/model and have nonzero norm")

    @property
    def vector_hash(self):
        if self.record is None:
            return "tombstone"
        return hashlib.sha256(struct.pack(f"<{self.model.dimensions}f", *self.record.vector)).hexdigest()


class VersionedVectorBackend(Protocol):
    model: VectorModel
    sync_target_id: str

    def apply_mutation(self, mutation: VectorMutation) -> int:
        """Durable remote revision >= requested; equal revisions must have identical payload.

        Must reject stale writes at the remote storage boundary, including after
        timeout/lease takeover. Tombstones cannot be physically deleted while
        old events may exist. No unversioned writes to this namespace allowed.
        """
        ...
