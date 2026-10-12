"""Small metadata-only maintenance responses; never expose remote payload text."""

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from app.chat.vector_sync_contracts import MAX_SYNC_REVISION, VectorMutation, VersionedVectorBackend


def cursor_key(value):
    if type(value) is int and 0 <= value <= 18_446_744_073_709_551_615:
        return (0, value)
    if isinstance(value, str) and str(UUID(value)) == value:
        return (1, UUID(value).int)
    raise ValueError("invalid remote point cursor")


@dataclass(frozen=True, slots=True)
class VectorInspection:
    memory_id: UUID
    code: Literal["current", "missing", "drift", "ahead", "scope_conflict", "invalid_revision"]
    remote_revision: int | None = None

    def __post_init__(self):
        if (not isinstance(self.memory_id, UUID) or self.code not in (
                "current", "missing", "drift", "ahead", "scope_conflict", "invalid_revision")
                or (self.remote_revision is not None and (type(self.remote_revision) is not int
                    or not 1 <= self.remote_revision <= MAX_SYNC_REVISION))):
            raise ValueError("invalid vector inspection")


@dataclass(frozen=True, slots=True)
class RemoteIdentity:
    point_id: str | int
    room_id: UUID | None = None
    memory_id: UUID | None = None

    def __post_init__(self):
        cursor_key(self.point_id)
        if (self.room_id is None) != (self.memory_id is None) or any(
                value is not None and not isinstance(value, UUID) for value in (self.room_id, self.memory_id)):
            raise ValueError("invalid remote identity")


@dataclass(frozen=True, slots=True)
class RemotePage:
    points: tuple[RemoteIdentity, ...]
    next_cursor: str | int | None

    def __post_init__(self):
        if not isinstance(self.points, tuple) or len(self.points) > 16 or any(
                not isinstance(point, RemoteIdentity) for point in self.points):
            raise ValueError("invalid remote page")
        keys = [cursor_key(point.point_id) for point in self.points]
        if keys != sorted(set(keys)):
            raise ValueError("remote page must be unique and ordered")
        if self.next_cursor is not None:
            next_key = cursor_key(self.next_cursor)
            if not keys or next_key <= keys[-1]:
                raise ValueError("nonadvancing remote cursor")


class MaintainableVectorBackend(VersionedVectorBackend, Protocol):
    def inspect_mutations(self, mutations: tuple[VectorMutation, ...]) -> tuple[VectorInspection, ...]: ...

    def scan_identities(self, cursor=None, *, limit=16) -> RemotePage: ...
