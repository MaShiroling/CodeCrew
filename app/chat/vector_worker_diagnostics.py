"""Read-only bounded aggregate output. Never select facts/vectors/lease tokens.

Event enqueue/ACK time is persisted by migration 29's transactional triggers.
Legacy rows are unknown, not guessed from retry availability or migration time.
Counts describe retained rows/current error labels, not lifetime failure counters.
"""

import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from app.chat.vector_worker_telemetry_schema import VECTOR_WORKER_TELEMETRY_MIGRATION

STATUSES = ("pending", "processing", "waiting", "dead", "delivered", "superseded")
ERROR_CODES = ("delivery_failed", "lease_exhausted", "vector_missing_or_stale", "vector_invalid")


def read_vector_worker_diagnostics(path: Path, target_id: str, *, now=None):
    if not isinstance(path, Path):
        raise TypeError("typed diagnostics database path required")
    if (not isinstance(target_id, str) or len(target_id) != 64
            or any(c not in "0123456789abcdef" for c in target_id)):
        raise ValueError("non-secret diagnostics target identity required")
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
        raise ValueError("finite nonnegative diagnostics time required")
    # Existing read-only file only. No migrations/claims/PRAGMA journal changes.
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=.1)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        registered = conn.execute("SELECT count(*) FROM standalone_chat_vector_targets WHERE target_id=?",
                                  (target_id,)).fetchone()[0] == 1
        counts = {s: 0 for s in STATUSES}
        for status, count in conn.execute("SELECT status,count(*) FROM standalone_chat_vector_outbox "
                                          "WHERE target_id=? GROUP BY status", (target_id,)):
            if status not in counts:
                raise ValueError("invalid diagnostics status")
            counts[status] = count
        pending_ready = conn.execute("SELECT count(*) FROM standalone_chat_vector_outbox "
            "WHERE target_id=? AND status='pending' AND available_at<=?", (target_id, now)).fetchone()[0]
        expired = conn.execute("SELECT count(*) FROM standalone_chat_vector_outbox "
            "WHERE target_id=? AND status='processing' AND lease_until<=?", (target_id, now)).fetchone()[0]
        # SQL maps unknown text to a constant before it can enter Python/logs.
        marks = ",".join("?" for _ in ERROR_CODES)
        failures = dict(conn.execute(f"SELECT CASE WHEN error_code IN ({marks}) THEN error_code ELSE 'other' END,count(*) "
            "FROM standalone_chat_vector_outbox WHERE target_id=? AND error_code IS NOT NULL "
            "AND status IN ('pending','processing','waiting','dead') GROUP BY 1",
            (*ERROR_CODES, target_id)))
        migration = conn.execute("SELECT name,checksum FROM schema_migrations WHERE version=29").fetchone()
        timing_available = migration == (VECTOR_WORKER_TELEMETRY_MIGRATION.name, VECTOR_WORKER_TELEMETRY_MIGRATION.checksum)
        backlog = sum(counts[s] for s in ("pending", "processing", "waiting", "dead"))
        known = skew = delivered_known = delivered_skew = 0
        oldest = last = None
        if timing_available:
            known, oldest, skew = conn.execute("""SELECT
                sum(CASE WHEN t.enqueued_at>=0 AND t.enqueued_at<=? THEN 1 ELSE 0 END),
                min(CASE WHEN t.enqueued_at>=0 AND t.enqueued_at<=? THEN t.enqueued_at END),
                sum(CASE WHEN t.enqueued_at>? THEN 1 ELSE 0 END)
                FROM standalone_chat_vector_outbox o LEFT JOIN standalone_chat_vector_event_times t ON t.event_id=o.event_id
                WHERE o.target_id=? AND o.status IN ('pending','processing','waiting','dead')""",
                (now, now, now, target_id)).fetchone()
            known, skew = known or 0, skew or 0
            delivered_known, last, delivered_skew = conn.execute("""SELECT
                sum(CASE WHEN t.delivered_at>=0 AND t.delivered_at<=? THEN 1 ELSE 0 END),
                max(CASE WHEN t.delivered_at>=0 AND t.delivered_at<=? THEN t.delivered_at END),
                sum(CASE WHEN t.delivered_at>? THEN 1 ELSE 0 END)
                FROM standalone_chat_vector_outbox o LEFT JOIN standalone_chat_vector_event_times t ON t.event_id=o.event_id
                WHERE o.target_id=? AND o.status='delivered'""", (now, now, now, target_id)).fetchone()
            delivered_known, delivered_skew = delivered_known or 0, delivered_skew or 0
        if any(v is not None and (not math.isfinite(v) or v < 0) for v in (oldest, last)):
            raise ValueError("invalid diagnostics timestamps")
    return {"observed_at_unix": now, "target_registered": registered, "status_counts": counts,
            "backlog_total": backlog, "ready_pending_count": pending_ready, "expired_processing_count": expired,
            "current_error_counts": failures, "timing_metadata_available": timing_available,
            "backlog_age_known_count": known, "backlog_age_unknown_count": backlog-known,
            "backlog_clock_skew_count": skew,
            "oldest_known_backlog_age_seconds": now-oldest if oldest is not None else None,
            "delivery_time_unknown_count": counts["delivered"]-delivered_known,
            "delivery_clock_skew_count": delivered_skew,
            "last_delivered_at_unix": last, "seconds_since_last_delivery": now-last if last is not None else None}


def safe_worker_diagnostics(worker):
    """Optional telemetry failures cannot turn a committed delivery into failure."""
    try:
        return {"diagnostics": worker.diagnostics(), "diagnostics_status": "observed"}
    except Exception:  # noqa: BLE001 - only fixed category, retain no SQL/body/path
        return {"diagnostics": None, "diagnostics_status": "unavailable"}
