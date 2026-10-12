"""Opt-in background delivery contracts only; no process, loop or signal hooks.

SQLite/cached vectors remain authoritative. A readiness probe is explicit and
read-only; it cannot migrate, initialize, subscribe, repair, retry dead or embed.
The future runtime must stop claiming between events and keep uncertain leases.
"""

import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from app.chat.vector_contracts import VectorModel
from app.chat.vector_outbox_schema import VECTOR_OUTBOX_MIGRATION
from app.chat.vector_sync_contracts import VersionedVectorBackend

if TYPE_CHECKING:
    from app.config import Settings


def _finite_timing(value, *, minimum, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("invalid worker timing")


def _target_identity(value):
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)):
        raise ValueError("invalid non-secret worker target identity")


@dataclass(frozen=True, slots=True)
class VectorWorkerPolicy:
    batch_size: int = 32
    poll_interval_seconds: float = 2.0
    shutdown_grace_seconds: float = 10.0

    def __post_init__(self):
        if type(self.batch_size) is not int or not 1 <= self.batch_size <= 200:
            raise ValueError("worker batch size must be within 1–200")
        _finite_timing(self.poll_interval_seconds, minimum=.1, maximum=60)
        _finite_timing(self.shutdown_grace_seconds, minimum=.1, maximum=60)


def requested_vector_worker_policy(settings: "Settings", *, requested: bool = False) -> VectorWorkerPolicy | None:
    """Pure double opt-in; ordinary chat startup must not even prepare a target.

    Worker configuration does NOT require Qdrant recall or cloud embedding on:
    dedicated delivery may consume existing cached vectors while chat uses SQLite.
    """
    if type(requested) is not bool:
        raise ValueError("explicit worker request must be bool")
    if not requested:
        return None
    if settings.memory_vector_worker_enabled is not True:
        raise ValueError("worker watch requires CODECREW_MEMORY_VECTOR_WORKER_ENABLED=true")
    return VectorWorkerPolicy(settings.memory_vector_worker_batch_size,
                              settings.memory_vector_worker_poll_interval_seconds,
                              settings.memory_vector_worker_shutdown_grace_seconds)


@dataclass(frozen=True, slots=True)
class VectorWorkerStopRequest:
    """A monotonic stop window; repeated signals must preserve the first window.

    Expiry means no clean shutdown can be claimed, not permission to ACK/release
    an uncertain in-flight claim. Runtime/supervisor termination is a later step.
    """
    requested_at: float
    grace_seconds: float

    def __post_init__(self):
        _finite_timing(self.requested_at, minimum=0, maximum=1e12)
        _finite_timing(self.grace_seconds, minimum=.1, maximum=60)

    def remaining_seconds(self, now: float) -> float:
        _finite_timing(now, minimum=self.requested_at, maximum=1e12)
        return max(0., self.grace_seconds - (now-self.requested_at))


@dataclass(frozen=True, slots=True)
class PreparedVectorWorkerTarget:
    database_path: Path = field(repr=False)
    model: VectorModel
    target_id: str

    def __post_init__(self):
        if not isinstance(self.database_path, Path) or not self.database_path.is_absolute():
            raise ValueError("absolute typed worker database path required")
        if not isinstance(self.model, VectorModel):
            raise TypeError("typed worker model required")
        _target_identity(self.target_id)


class ReadyVersionedVectorBackend(VersionedVectorBackend, Protocol):
    def _require_sync_ready(self) -> None:
        """Fresh metadata-only compatibility check; no remote creation/repair."""
        ...


class VectorWorkerStartError(RuntimeError):
    """Fixed startup category only; never propagate DB/provider bodies or paths."""


def check_vector_worker_target(database_path: Path, backend: ReadyVersionedVectorBackend) -> PreparedVectorWorkerTarget:
    """Explicit local read-only snapshot then fresh remote GET; no claims/writes.

    The caller must already hold a requested policy and explicitly construct the
    versioned backend with a selected model. Successful startup is an observation,
    not a permanent ready token: the backend validates readiness on each delivery.
    """
    if not isinstance(database_path, Path) or not isinstance(backend.model, VectorModel):
        raise TypeError("typed explicit worker database and model required")
    _target_identity(backend.sync_target_id)
    try:
        path = database_path.resolve(strict=True)
        if not path.is_file():
            raise VectorWorkerStartError("database_not_file")
    except OSError:
        raise VectorWorkerStartError("database_unavailable") from None
    try:
        # mode=ro prevents creating an absent database or changing canonical data.
        # URI escaping is supplied by Path.as_uri(); never interpolate raw paths.
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            migration = conn.execute("SELECT name, checksum FROM schema_migrations WHERE version=?",
                                     (VECTOR_OUTBOX_MIGRATION.version,)).fetchone()
            if migration != (VECTOR_OUTBOX_MIGRATION.name, VECTOR_OUTBOX_MIGRATION.checksum):
                raise VectorWorkerStartError("outbox_schema_not_prepared")
            # Require the delivery schema without scanning event/fact/vector payloads.
            conn.execute("SELECT event_id,target_id,memory_id,revision,status,attempts,available_at,"
                         "lease_until,lease_token,error_code FROM standalone_chat_vector_outbox LIMIT 0")
            conn.execute("SELECT memory_id,room_id,revision,tombstone FROM standalone_chat_vector_revisions LIMIT 0")
            target = conn.execute("SELECT model_fingerprint,dimensions FROM standalone_chat_vector_targets WHERE target_id=?",
                                  (backend.sync_target_id,)).fetchone()
            if target != (backend.model.fingerprint, backend.model.dimensions):
                raise VectorWorkerStartError("target_not_prepared")
            meta = conn.execute("SELECT dimensions FROM standalone_chat_embedding_meta WHERE model_fingerprint=?",
                                (backend.model.fingerprint,)).fetchone()
            # No cached model yet is allowed: absent vectors become waiting, never cloud work.
            if meta is not None and meta != (backend.model.dimensions,):
                raise VectorWorkerStartError("cache_model_mismatch")
    except sqlite3.Error:
        raise VectorWorkerStartError("database_schema_unavailable") from None
    # No SQLite snapshot/transaction is held across HTTP. Never call initialize().
    try:
        backend._require_sync_ready()
    except Exception:  # noqa: BLE001 - fixed safe category, no provider messages
        raise VectorWorkerStartError("remote_not_ready") from None
    return PreparedVectorWorkerTarget(path, backend.model, backend.sync_target_id)
