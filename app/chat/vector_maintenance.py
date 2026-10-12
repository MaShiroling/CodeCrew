"""Resumable bounded backfill, two-way audit, and outbox-only drift repair.

No model calls, canonical fact edits, collection deletion or automatic cutover.
Checkpoints and enqueues commit together; remote reads are outside write locks.
"""

import json
from uuid import UUID, uuid4

from app.chat.vector_maintenance_contracts import RemotePage, VectorInspection, cursor_key
from app.chat.vector_outbox import MemoryVectorOutbox


class VectorMaintenanceError(RuntimeError):
    """Safe failure category; never retain provider errors or payloads."""


class MemoryVectorMaintenance:
    def __init__(self, worker: MemoryVectorOutbox):
        self.worker, self.database, self.backend = worker, worker.database, worker.backend

    def _run(self, conn, run_id):
        if not isinstance(run_id, UUID):
            raise TypeError("maintenance run UUID required")
        self.worker._require_target(conn)
        row = conn.execute("SELECT * FROM standalone_chat_vector_maintenance_runs WHERE run_id=? AND target_id=?",
                           (str(run_id), self.worker.target_id)).fetchone()
        if row is None:
            raise ValueError("maintenance run not found for this target")
        return dict(row)

    def _event_id(self, conn):
        return conn.execute("SELECT COALESCE(MAX(event_id),0) FROM standalone_chat_vector_outbox WHERE target_id=?",
                            (self.worker.target_id,)).fetchone()[0]

    def start(self, *, mode="backfill") -> UUID:
        if mode not in ("backfill", "audit", "repair"):
            raise ValueError("invalid vector maintenance mode")
        run_id = uuid4()
        with self.database.transaction() as conn:
            self.worker._require_target(conn)
            high_water = conn.execute("SELECT COALESCE(MAX(memory_id),'') FROM standalone_chat_vector_revisions").fetchone()[0]
            conn.execute("INSERT INTO standalone_chat_vector_maintenance_runs "
                         "(run_id,target_id,mode,high_water,start_event_id) VALUES (?,?,?,?,?)",
                         (str(run_id), self.worker.target_id, mode, high_water, self._event_id(conn)))
        return run_id

    @staticmethod
    def _limit(limit):
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError("maintenance page must be within 1–16")

    @staticmethod
    def _count(counts, code, n=1):
        counts[code] = counts.get(code, 0) + n

    @staticmethod
    def _finding(conn, run, phase, item_id, code, revision=None):
        conn.execute("INSERT OR IGNORE INTO standalone_chat_vector_maintenance_findings VALUES (?,?,?,?,?)",
                     (run["run_id"], phase, str(item_id), code, revision))

    def _read_page(self, run_id, limit):
        with self.database.connect() as conn:
            conn.execute("BEGIN")
            run = self._run(conn, run_id)
            if run["phase"] != "local":
                return run, ()
            rows = conn.execute("SELECT * FROM standalone_chat_vector_revisions "
                                "WHERE memory_id>? AND memory_id<=? ORDER BY memory_id LIMIT ?",
                                (run["local_cursor"], run["high_water"], limit)).fetchall()
            return run, tuple(self.worker.desired_in_snapshot(conn, row) for row in rows)

    def _inspect(self, desired):
        mutations = tuple(mutation for mutation, _ in desired)
        observations = self.backend.inspect_mutations(mutations)
        if (not isinstance(observations, tuple) or len(observations) != len(mutations)
                or any(not isinstance(item, VectorInspection) or item.memory_id != mutation.memory_id
                       for item, mutation in zip(observations, mutations, strict=True))):
            raise VectorMaintenanceError("invalid vector maintenance inspection")
        return observations

    def _enqueue(self, conn, memory_id, revision):
        return conn.execute("INSERT OR IGNORE INTO standalone_chat_vector_outbox "
                            "(target_id,memory_id,revision) VALUES (?,?,?)",
                            (self.worker.target_id, str(memory_id), revision)).rowcount

    def _repair(self, conn, mutation):
        # Caller has revalidated the canonical revision in this write transaction.
        # Increment derivative ordering only, never edit the canonical fact.
        conn.execute("UPDATE standalone_chat_vector_revisions SET revision=revision+1 WHERE memory_id=?",
                     (str(mutation.memory_id),))
        conn.execute("INSERT INTO standalone_chat_vector_outbox(target_id,memory_id,revision) "
                     "SELECT t.target_id,r.memory_id,r.revision FROM standalone_chat_vector_targets t "
                     "JOIN standalone_chat_vector_revisions r ON r.memory_id=?", (str(mutation.memory_id),))

    def step(self, run_id: UUID, *, limit=16):
        """One bounded page; resume with the same run ID after crash/failure.

        Concurrent callers may repeat remote reads, but checkpoint CAS permits
        only one to commit counters/repairs. Failures never advance a checkpoint.
        """
        self._limit(limit)
        run = None
        try:
            run, desired = self._read_page(run_id, limit)
            if run["phase"] == "done":
                return self.status(run_id)
            observations, remote_page = (), None
            if run["phase"] == "remote":
                cursor = json.loads(run["remote_cursor"]) if run["remote_cursor"] is not None else None
                remote_page = self.backend.scan_identities(cursor, limit=limit)
                if not isinstance(remote_page, RemotePage) or len(remote_page.points) > limit:
                    raise VectorMaintenanceError("invalid remote maintenance page")
                if cursor is not None and any(cursor_key(item.point_id) < cursor_key(cursor) for item in remote_page.points):
                    raise VectorMaintenanceError("remote maintenance cursor went backwards")
            elif run["mode"] != "backfill" and desired:
                observations = self._inspect(desired)
            with self.database.transaction() as conn:
                current_run = self._run(conn, run_id)
                if current_run["checkpoint"] != run["checkpoint"]:
                    return self._status_in_snapshot(conn, current_run)
                counts = json.loads(run["counts_json"])
                phase, local_cursor, remote_cursor = run["phase"], run["local_cursor"], run["remote_cursor"]
                if phase == "local":
                    for index, (mutation, waiting) in enumerate(desired):
                        self._count(counts, "local_scanned")
                        local_cursor = str(mutation.memory_id)
                        row = conn.execute("SELECT revision,room_id FROM standalone_chat_vector_revisions WHERE memory_id=?",
                                           (str(mutation.memory_id),)).fetchone()
                        if row is None or row["revision"] != mutation.revision or row["room_id"] != str(mutation.room_id):
                            self._count(counts, "changed_during_page")
                            self._finding(conn, run, "local", mutation.memory_id, "changed_during_page")
                            continue
                        if waiting:
                            self._count(counts, "cache_waiting")
                            self._finding(conn, run, "local", mutation.memory_id, waiting)
                        if run["mode"] == "backfill":
                            self._count(counts, "enqueued", self._enqueue(conn, mutation.memory_id, mutation.revision))
                            continue
                        observation = observations[index]
                        self._count(counts, observation.code)
                        if observation.code != "current":
                            self._finding(conn, run, "local", mutation.memory_id, observation.code, observation.remote_revision)
                        if run["mode"] == "repair" and observation.code in ("missing", "drift"):
                            self._repair(conn, mutation)
                            self._count(counts, "repair_enqueued")
                    if not desired or local_cursor == run["high_water"]:
                        phase = "done" if run["mode"] == "backfill" else "remote"
                else:
                    for item in remote_page.points:
                        self._count(counts, "remote_scanned")
                        code = None
                        if item.memory_id is None:
                            code = "unmanaged"
                        else:
                            row = conn.execute("SELECT room_id FROM standalone_chat_vector_revisions WHERE memory_id=?",
                                               (str(item.memory_id),)).fetchone()
                            if row is None or row["room_id"] != str(item.room_id):
                                code = "orphan"
                            elif str(item.memory_id) > run["high_water"]:
                                code = "outside_window"
                        if code:
                            self._count(counts, code)
                            self._finding(conn, run, "remote", item.point_id, code)
                    remote_cursor = json.dumps(remote_page.next_cursor)
                    if remote_page.next_cursor is None:
                        phase = "done"
                conn.execute("UPDATE standalone_chat_vector_maintenance_runs SET phase=?,local_cursor=?,remote_cursor=?, "
                             "checkpoint=checkpoint+1,counts_json=?,error_code=NULL WHERE run_id=?",
                             (phase, local_cursor, remote_cursor, json.dumps(counts), str(run_id)))
                return self._status_in_snapshot(conn, self._run(conn, run_id))
        except Exception:  # noqa: BLE001 - safe fixed category, remote response never persisted
            if run is not None:
                with self.database.transaction() as conn:
                    conn.execute("UPDATE standalone_chat_vector_maintenance_runs SET error_code='maintenance_failed' "
                                 "WHERE run_id=? AND checkpoint=? AND target_id=?",
                                 (str(run_id), run["checkpoint"], self.worker.target_id))
            raise VectorMaintenanceError("vector maintenance page failed; checkpoint preserved") from None

    def _status_in_snapshot(self, conn, run):
        counts = json.loads(run["counts_json"])
        changed = self._event_id(conn) != run["start_event_id"]
        outbox = {row["status"]: row["n"] for row in conn.execute(
            "SELECT status,count(*) AS n FROM standalone_chat_vector_outbox WHERE target_id=? GROUP BY status",
            (self.worker.target_id,))}
        problems = ("cache_waiting", "missing", "drift", "ahead", "scope_conflict", "invalid_revision",
                    "changed_during_page", "orphan", "unmanaged", "outside_window")
        consistent = (run["mode"] == "audit" and run["phase"] == "done" and not changed
                      and not run["error_code"] and not any(counts.get(code, 0) for code in problems)
                      and not any(outbox.get(state, 0) for state in ("pending", "processing", "waiting", "dead")))
        return {"run_id": run["run_id"], "mode": run["mode"], "phase": run["phase"],
                "scan_complete": run["phase"] == "done", "consistent_at_observation": consistent,
                "changed_since_start": changed, "checkpoint": run["checkpoint"],
                "counts": counts, "outbox": outbox, "error_code": run["error_code"],
                "embedding_calls": 0, "agent_calls": 0}

    def status(self, run_id):
        with self.database.connect() as conn:
            conn.execute("BEGIN")
            return self._status_in_snapshot(conn, self._run(conn, run_id))

    def findings(self, run_id, *, limit=16):
        self._limit(limit)
        with self.database.connect() as conn:
            self._run(conn, run_id)
            return tuple(dict(row) for row in conn.execute(
                "SELECT phase,item_id,code,remote_revision FROM standalone_chat_vector_maintenance_findings "
                "WHERE run_id=? ORDER BY phase,item_id,code LIMIT ?", (str(run_id), limit)))
