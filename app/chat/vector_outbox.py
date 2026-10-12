"""Bounded durable delivery of cached vectors; no model calls or background startup.

Claims are leased/fenced in short SQLite transactions. Network calls run outside
them; server-side monotonic revisions prevent delayed requests from resurrecting
retired data. Explicitly registered targets receive future changes only (step 4
will add historical backfill). Canonical SQLite remains authoritative.
"""

import hashlib
import json
import math
import struct
import time
from dataclasses import dataclass
from uuid import UUID, uuid4

from app.chat.memory import _SENSITIVE
from app.chat.vector_contracts import VectorAnchor, VectorRecord
from app.chat.vector_sync_contracts import MAX_SYNC_REVISION, VectorMutation, VersionedVectorBackend
from app.storage import SQLiteDatabase


@dataclass(frozen=True, slots=True)
class SyncPolicy:
    max_attempts: int = 5
    lease_seconds: float = 60.0
    retry_base_seconds: float = 2.0
    retry_cap_seconds: float = 60.0

    def __post_init__(self):
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 20:
            raise ValueError("invalid sync attempt limit")
        for name in ("lease_seconds", "retry_base_seconds", "retry_cap_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 3600:
                raise ValueError("invalid sync timing")
        if self.retry_cap_seconds < self.retry_base_seconds:
            raise ValueError("sync retry cap must be at least the base delay")


@dataclass(frozen=True, slots=True)
class SyncClaim:
    event_id: int
    memory_id: UUID
    revision: int
    attempts: int
    token: str

    def __post_init__(self):
        if (type(self.event_id) is not int or self.event_id < 1
                or not isinstance(self.memory_id, UUID)
                or type(self.revision) is not int or not 1 <= self.revision <= MAX_SYNC_REVISION
                or type(self.attempts) is not int or not 1 <= self.attempts <= 20
                or not isinstance(self.token, str) or len(self.token) != 32
                or any(char not in "0123456789abcdef" for char in self.token)):
            raise ValueError("invalid synchronization claim")


class MemoryVectorOutbox:
    def __init__(self, database: SQLiteDatabase, backend: VersionedVectorBackend, *,
                 policy: SyncPolicy | None = None, clock=time.time):
        self.database, self.backend = database, backend
        self.policy, self.clock = policy or SyncPolicy(), clock
        target = backend.sync_target_id
        if not isinstance(target, str) or len(target) != 64 or any(
                char not in "0123456789abcdef" for char in target):
            raise ValueError("sync target must be a non-secret SHA-256 identity")
        self.target_id = target

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid sync clock")
        return value

    def register_target(self):
        """Explicit local subscription; no historical scan or remote creation."""
        with self.database.transaction() as conn:
            existing = conn.execute("SELECT * FROM standalone_chat_vector_targets WHERE target_id=?",
                                    (self.target_id,)).fetchone()
            model = self.backend.model
            if existing is not None and (existing["model_fingerprint"] != model.fingerprint
                                         or existing["dimensions"] != model.dimensions):
                raise ValueError("sync target model changed without a new identity")
            conn.execute("INSERT INTO standalone_chat_vector_targets VALUES (?, ?, ?) "
                         "ON CONFLICT(target_id) DO NOTHING",
                         (self.target_id, model.fingerprint, model.dimensions))

    def _require_target(self, conn):
        target = conn.execute("SELECT * FROM standalone_chat_vector_targets WHERE target_id=?",
                              (self.target_id,)).fetchone()
        if (target is None or target["model_fingerprint"] != self.backend.model.fingerprint
                or target["dimensions"] != self.backend.model.dimensions):
            raise ValueError("synchronization target not registered or model mismatch")

    def claim(self, *, should_stop=None) -> SyncClaim | None:
        if should_stop is not None and not callable(should_stop):
            raise TypeError("stop checkpoint must be callable")
        if should_stop is not None and should_stop():
            return None
        now = self._now()
        with self.database.transaction() as conn:
            # Recheck after waiting for SQLite's write lock, not only before it.
            if should_stop is not None and should_stop():
                return None
            self._require_target(conn)
            # Coalesce old pointers, not immutable fact snapshots. Includes dead/
            # waiting and expired/in-flight claims; their token can no longer ACK.
            conn.execute("""UPDATE standalone_chat_vector_outbox SET status='superseded',
                lease_token=NULL, lease_until=NULL WHERE target_id=?
                AND status IN ('pending','processing','waiting','dead')
                AND revision < (SELECT revision FROM standalone_chat_vector_revisions r
                                WHERE r.memory_id=standalone_chat_vector_outbox.memory_id)""",
                         (self.target_id,))
            conn.execute("""UPDATE standalone_chat_vector_outbox SET status='dead',
                lease_token=NULL, lease_until=NULL, error_code='lease_exhausted'
                WHERE target_id=? AND status='processing' AND lease_until<=? AND attempts>=?""",
                         (self.target_id, now, self.policy.max_attempts))
            row = conn.execute("""SELECT * FROM standalone_chat_vector_outbox
                WHERE target_id=? AND attempts<? AND
                    ((status='pending' AND available_at<=?) OR
                     (status='processing' AND lease_until<=?))
                ORDER BY event_id LIMIT 1""",
                               (self.target_id, self.policy.max_attempts, now, now)).fetchone()
            if row is None:
                return None
            if should_stop is not None and should_stop():
                return None
            token = uuid4().hex
            conn.execute("""UPDATE standalone_chat_vector_outbox SET status='processing',
                attempts=attempts+1, lease_token=?, lease_until=?, error_code=NULL WHERE event_id=?""",
                         (token, now + self.policy.lease_seconds, row["event_id"]))
            return SyncClaim(row["event_id"], UUID(row["memory_id"]), row["revision"],
                             row["attempts"] + 1, token)

    def _materialize(self, claim):
        with self.database.connect() as conn:
            conn.execute("BEGIN")
            self._require_target(conn)
            event = conn.execute("SELECT status, lease_token FROM standalone_chat_vector_outbox "
                                 "WHERE event_id=? AND target_id=?",
                                 (claim.event_id, self.target_id)).fetchone()
            current = conn.execute("SELECT * FROM standalone_chat_vector_revisions WHERE memory_id=?",
                                   (str(claim.memory_id),)).fetchone()
            if (event is None or event["status"] != "processing" or event["lease_token"] != claim.token
                    or current is None or current["revision"] != claim.revision):
                return None, "superseded"
            return self.desired_in_snapshot(conn, current)

    def desired_in_snapshot(self, conn, current):
        """Shared canonical/cache validation for delivery and bounded maintenance.

        Caller owns the read snapshot; returned data is revalidated by revision
        before maintenance writes. No event/lease is fabricated for an audit.
        """
        memory_id = UUID(current["memory_id"])
        mutation = VectorMutation(UUID(current["room_id"]), memory_id, current["revision"],
                                  self.backend.model)
        row = conn.execute("""SELECT m.*, s.correlation_id, s.message_json, member.role,
                s.room_id AS source_room_id, member.room_id AS member_room_id,
                e.model_fingerprint, e.content_hash, e.dimensions, e.vector
                FROM standalone_chat_memories m
                JOIN standalone_chat_messages s ON s.message_id=m.source_message_id
                JOIN standalone_chat_members member ON member.member_id=s.sender_id
                LEFT JOIN standalone_chat_memory_embeddings e ON e.memory_id=m.memory_id
                WHERE m.memory_id=? AND m.room_id=?""",
                           (str(memory_id), current["room_id"])).fetchone()
        root = None
        if not current["tombstone"] and row is not None and row["status"] == "active":
            root = conn.execute("SELECT message_json, room_id FROM standalone_chat_messages "
                                "WHERE correlation_id=? ORDER BY sequence LIMIT 1",
                                (row["correlation_id"],)).fetchone()
        return self._desired_from_rows(current, row, root, mutation=mutation)

    def desired_many_in_snapshot(self, conn, currents):
        """Two bounded SELECTs, same validation as delivery; caller owns snapshot.

        Read-only: no subscription, event, embedding or network work. Return by
        identity, never depend on SQL IN/GROUP BY output order. The root is the
        globally earliest sequence for each requested correlation, as in the
        single-row path; do not filter roots by room and hide a foreign root.
        """
        currents = tuple(currents)
        if len(currents) > 200:
            raise ValueError("canonical batch must be within 0–200")
        ids = tuple(str(UUID(row["memory_id"])) for row in currents)
        if len(set(ids)) != len(ids):
            raise ValueError("canonical batch identities must be distinct")
        if not conn.in_transaction:
            raise ValueError("canonical batch requires an existing snapshot")
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = {row["memory_id"]: row for row in conn.execute(f"""
            SELECT m.*, s.correlation_id, s.message_json, member.role,
                s.room_id AS source_room_id, member.room_id AS member_room_id,
                e.model_fingerprint, e.content_hash, e.dimensions, e.vector
            FROM standalone_chat_memories m
            JOIN standalone_chat_messages s ON s.message_id=m.source_message_id
            JOIN standalone_chat_members member ON member.member_id=s.sender_id
            LEFT JOIN standalone_chat_memory_embeddings e ON e.memory_id=m.memory_id
            WHERE m.memory_id IN ({placeholders})""", ids)}
        selected = []
        for current, memory_id in zip(currents, ids, strict=True):
            row = rows.get(memory_id)
            if row is not None and row["room_id"] != current["room_id"]:
                row = None
            selected.append((current, row))
        # Only active canonical records would read/decode a root in the old path.
        correlations = tuple(dict.fromkeys(row["correlation_id"] for current, row in selected
            if row is not None and not current["tombstone"] and row["status"] == "active"))
        roots = {}
        if correlations:
            placeholders = ",".join("?" for _ in correlations)
            roots = {row["correlation_id"]: row for row in conn.execute(f"""
                SELECT root.correlation_id, root.message_json, root.room_id
                FROM standalone_chat_messages root
                JOIN (SELECT correlation_id, MIN(sequence) AS first_sequence
                      FROM standalone_chat_messages WHERE correlation_id IN ({placeholders})
                      GROUP BY correlation_id) first ON root.sequence=first.first_sequence""", correlations)}
        result = {}
        for current, row in selected:
            result[current["memory_id"]] = self._desired_from_rows(
                current, row, roots.get(row["correlation_id"]) if row is not None else None)
        return result

    def _desired_from_rows(self, current, row, root, *, mutation=None):
        """Pure shared source/cache/contract checks; no SQL or side effects."""
        memory_id = UUID(current["memory_id"])
        mutation = mutation or VectorMutation(UUID(current["room_id"]), memory_id, current["revision"],
                                              self.backend.model)
        if current["tombstone"] or row is None or row["status"] != "active":
            return mutation, None
        source = json.loads(row["message_json"])
        if (row["source_room_id"] != current["room_id"]
                    or row["member_room_id"] != current["room_id"]
                    or row["role"] != "human" or source.get("external_source") is not None
                    or root is None or json.loads(root["message_json"]).get("external_source") is not None
                    or root["room_id"] != current["room_id"]
                    or _SENSITIVE.search(row["content"]) or _SENSITIVE.search(source["content"])):
            return mutation, None  # Remove derived data, never send sensitive text.
        model = self.backend.model
        digest = hashlib.sha256(row["content"].encode()).hexdigest()
        if (row["vector"] is None or row["model_fingerprint"] != model.fingerprint
                    or row["dimensions"] != model.dimensions or row["content_hash"] != digest):
                # Remove stale remote data as well; a subsequent cache write
                # generates a NEW revision and resumes with a valid vector.
            return mutation, "vector_missing_or_stale"
        if not isinstance(row["vector"], bytes) or len(row["vector"]) != 4 * model.dimensions:
            return mutation, "vector_invalid"
        vector = struct.unpack(f"<{model.dimensions}f", row["vector"])
        if not all(math.isfinite(x) for x in vector) or not any(vector):
            return mutation, "vector_invalid"
        anchor = VectorAnchor(memory_id, mutation.room_id, UUID(row["source_message_id"]),
                              UUID(row["correlation_id"]), digest)
        return VectorMutation(mutation.room_id, memory_id, mutation.revision, model,
                              VectorRecord(anchor, model, vector)), None

    def _finish(self, claim, status, *, error_code=None, delay=0):
        with self.database.transaction() as conn:
            # A stale lease or changed canonical revision may not mark a newer
            # event delivered. The change itself has already enqueued that event.
            return conn.execute("""UPDATE standalone_chat_vector_outbox SET status=CASE
                WHEN revision < (SELECT revision FROM standalone_chat_vector_revisions r
                    WHERE r.memory_id=standalone_chat_vector_outbox.memory_id)
                THEN 'superseded' ELSE ? END,
                available_at=?, lease_token=NULL, lease_until=NULL, error_code=?
                WHERE event_id=? AND target_id=? AND status='processing' AND lease_token=?""",
                                (status, self._now() + delay, error_code,
                                 claim.event_id, self.target_id, claim.token)).rowcount == 1

    def deliver(self, claim: SyncClaim):
        if not isinstance(claim, SyncClaim):
            raise TypeError("typed synchronization claim required")
        try:
            mutation, waiting = self._materialize(claim)
            if mutation is None:
                self._finish(claim, "superseded")
                return
            revision = self.backend.apply_mutation(mutation)
            if type(revision) is not int or not mutation.revision <= revision <= MAX_SYNC_REVISION:
                raise ValueError("invalid remote revision receipt")
        except Exception:  # noqa: BLE001 - never store provider bodies/credentials/fact text
            exhausted = claim.attempts >= self.policy.max_attempts
            delay = min(self.policy.retry_cap_seconds,
                        self.policy.retry_base_seconds * 2 ** (claim.attempts - 1))
            self._finish(claim, "dead" if exhausted else "pending",
                         error_code="delivery_failed", delay=delay)
            return
        self._finish(claim, "waiting" if waiting else "delivered", error_code=waiting)

    def run_once(self, *, limit=32, should_stop=None) -> int:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("sync batch limit must be within 1–200")
        count = 0
        while count < limit:
            claim = self.claim(should_stop=should_stop)
            if claim is None:
                break
            self.deliver(claim)
            count += 1
        return count

    def retry_dead(self, *, limit=32) -> int:
        """Explicit bounded redrive; never automatic infinite retries."""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("redrive limit must be within 1–200")
        with self.database.transaction() as conn:
            self._require_target(conn)
            return conn.execute("""UPDATE standalone_chat_vector_outbox SET status='pending',
                attempts=0, available_at=0, error_code=NULL WHERE event_id IN
                (SELECT o.event_id FROM standalone_chat_vector_outbox o
                 JOIN standalone_chat_vector_revisions r ON r.memory_id=o.memory_id
                 WHERE o.target_id=? AND o.status='dead' AND o.revision=r.revision
                 ORDER BY o.event_id LIMIT ?)""", (self.target_id, limit)).rowcount

    def status(self):
        """Counts/categories only; no fact text, vectors, URL or API key."""
        with self.database.connect() as conn:
            rows = conn.execute("SELECT status, count(*) AS n FROM standalone_chat_vector_outbox "
                                "WHERE target_id=? GROUP BY status", (self.target_id,)).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def diagnostics(self):
        from app.chat.vector_worker_diagnostics import read_vector_worker_diagnostics
        return read_vector_worker_diagnostics(self.database.path, self.target_id)
