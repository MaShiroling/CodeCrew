"""Opt-in full optimized-read regression in one owned Docker; plan by default.

Two lexical modes, existing recovery journey plus actual query-response races,
owned index removal/recreation and outage between metadata gates. Synthetic
vectors only, no settings/cloud/Agent/shared Qdrant/real chat changes.
"""

import argparse
import hashlib
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from app.chat.qdrant_memory_index import SNAPSHOT_READ_REVISION
from app.chat.qdrant_vector_backend import QdrantEndpoint
from app.chat.qdrant_vector_sync import READINESS_CHECK_REVISION, QdrantVersionedVectorBackend
from app.chat.vector_contracts import VectorModel
from evals.check_memory_qdrant_faults import (
    IMAGE,
    ROOT,
    FaultJourney,
    OwnedDocker,
    SyntheticEmbedder,
    require,
)

RESULTS = ROOT / "evals/results/memory-qdrant-optimized-regression-v1"
MODES = ("legacy", "bm25")
RACES = ("retire", "replace", "complete", "cancel", "physical_delete", "cache", "external")
PROVENANCE = ("evals/check_memory_qdrant_optimized.py", "evals/check_memory_qdrant_faults.py",
              "app/chat/qdrant_memory_index.py", "app/chat/qdrant_vector_backend.py", "app/chat/qdrant_vector_sync.py",
              "app/chat/vector_outbox.py", "app/chat/vector_maintenance.py", "app/chat/memory.py",
              "app/chat/fts_memory.py", "app/chat/fts_text.py", "app/chat/sqlite_vector_backend.py",
              "app/chat/vector_contracts.py", "app/chat/vector_sync_contracts.py", "app/chat/store.py",
              "app/chat/service.py", "app/storage/sqlite.py")


class CountedDocker(OwnedDocker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stops = self.starts = 0

    def stop(self):
        super().stop()
        self.stops += 1

    def start(self):
        super().start()
        self.starts += 1


def concurrent_commit(change):
    """Independent thread/SQLite connection; bounded join, no raw errors logged."""
    errors = []
    def commit():
        try:
            change()
        except Exception as exc:  # noqa: BLE001 - never expose injected/provider/fact errors
            errors.append(type(exc).__name__)
    writer = threading.Thread(target=commit, daemon=True)
    writer.start()
    writer.join(timeout=5)
    require(not writer.is_alive(), "concurrent_writer_timeout")
    require(not errors, "concurrent_writer_failed")


class OptimizedJourney(FaultJourney):
    def __init__(self, *args, progress=print, **kwargs):
        super().__init__(*args, **kwargs)
        self.progress, self.boundaries = progress, []

    def new_room(self):
        return self.service.create_room(title="isolated read boundary", idempotency_key=uuid4()).room_id

    @contextmanager
    def observe(self, *, after_metadata=None, after_query=None):
        """Fixture-only instance hooks, restored on every exit. No raw SQL/URL/body."""
        request, snapshot = self.backend._request, self.index._snapshot
        prior = [(obj, name, name in vars(obj), vars(obj).get(name))
                 for obj, name in ((self.backend, "_request"), (self.index, "_snapshot"))]
        result = {"metadata_gets": 0, "query_posts": 0, "snapshot_selects": []}
        def checked_request(method, path, *args, **kwargs):
            metadata = method == "GET" and path == self.backend._path
            query = method == "POST" and path == self.backend._path + "/points/query"
            if metadata:
                result["metadata_gets"] += 1
            elif query:
                result["query_posts"] += 1
            else:
                raise RuntimeError("unexpected_read_boundary_endpoint")
            value = request(method, path, *args, **kwargs)
            if metadata and after_metadata:
                after_metadata(result["metadata_gets"], request)
            if query and after_query:
                after_query()
            return value
        def checked_snapshot(conn, *args, **kwargs):
            selects = [0]
            def trace(sql):
                if sql.lstrip().upper().startswith("SELECT"):
                    selects[0] += 1
            conn.set_trace_callback(trace)
            try:
                return snapshot(conn, *args, **kwargs)
            finally:
                conn.set_trace_callback(None)
                result["snapshot_selects"].append(selects[0])
        self.backend._request, self.index._snapshot = checked_request, checked_snapshot
        try:
            yield result
        finally:
            for obj, name, present, value in reversed(prior):
                if present:
                    setattr(obj, name, value)
                else:
                    delattr(obj, name)

    def record(self, name, observation):
        require(all(0 < count <= 5 for count in observation["snapshot_selects"]), "batch_read_bound_regressed")
        self.boundaries.append({"check": name, **observation})
        self.checks.append(name)
        self.progress(f"[{self.lexical_backend}] {name}", flush=True)

    def races(self):
        for action in RACES:
            room = self.new_room()
            memory = self.save("生命周期" + action, room=room, open_loop=action in ("complete", "cancel"))
            self.worker.run_once(limit=200)
            changed = [False]
            def mutate(action=action, room=room, memory=memory, changed=changed):
                changed[0] = True
                if action == "retire":
                    self.store.memory.retire(room, memory.memory_id)
                elif action == "replace":
                    self.save("替代事实", room=room, replaces=memory.memory_id, vector=(0., 0., 1.))
                elif action in ("complete", "cancel"):
                    self.service.close_open_loop(room, memory.memory_id, action=action)
                elif action == "cache":
                    self.cache(memory, vector=(0., 1., 0.))
                else:
                    with self.database.transaction() as conn:
                        if action == "external":
                            conn.execute("UPDATE standalone_chat_messages SET message_json="
                                         "json_set(message_json,'$.external_source','{}') WHERE message_id=?",
                                         (str(memory.source_message_id),))
                        else:
                            for table in ("standalone_chat_memory_embeddings", "standalone_chat_memory_fts_documents",
                                          "standalone_chat_memory_candidates", "standalone_chat_memories"):
                                conn.execute(f"DELETE FROM {table} WHERE memory_id=?", (str(memory.memory_id),))
            with self.observe(after_query=lambda change=mutate: concurrent_commit(change)) as observed:
                require(self.store.memory.retrieve(room, "semanticprobe") == (), "post_query_stale_hit_leaked")
            require(changed[0] and observed["metadata_gets"] == 2 and observed["query_posts"] == 1,
                    "post_query_race_not_exercised")
            if action == "external":
                with self.database.transaction() as conn:
                    conn.execute("UPDATE standalone_chat_messages SET message_json="
                                 "json_remove(message_json,'$.external_source') WHERE message_id=?",
                                 (str(memory.source_message_id),))
            self.worker.run_once(limit=200)
            self.record("actual_query_response_concurrent_" + action, observed)

    def schema_boundaries(self):
        room = self.new_room()
        memory = self.save("gateway 索引恢复样本", room=room)
        self.worker.run_once(limit=200)
        for field in ("room_id", "sync_revision"):
            for after_first in (False, True):
                # Direct original request is the deliberate fault injection,
                # not a read-path operation counted by observe().
                def remove(request, field=field):
                    if hasattr(self.control, "owned"):
                        require(self.control.owned() is not None, "owned_fault_target_missing")
                    self.backend._completed(request("DELETE", self.backend._path + f"/index/{field}?wait=true"))
                if not after_first:
                    remove(self.backend._request)
                before = self.embedder.calls
                try:
                    def after(n, original, enabled=after_first, remove=remove):
                        if n == 1 and enabled:
                            remove(original)
                    with self.observe(after_metadata=after) as observed:
                        require(self.store.memory.retrieve(room, "gateway") == (memory,), "schema_fault_lexical_fallback")
                    expected = 2 if after_first else 1
                    require(observed["metadata_gets"] == expected and observed["query_posts"] == 0
                            and self.embedder.calls - before == int(after_first), "schema_fault_not_at_expected_gate")
                    self.record(f"actual_index_missing_{field}_" + ("between_gates" if after_first else "before_vector"), observed)
                finally:
                    self.backend.initialize()  # Restore own fixture indexes only, not production auto-repair.
                with self.observe() as recovered:
                    require(self.store.memory.retrieve(room, "semanticprobe") == (memory,), "schema_recovery_missing")
                require(recovered["metadata_gets"] == 2 and recovered["query_posts"] == 1, "schema_recovery_cached_state")

        before = self.embedder.calls
        try:
            def outage(n, unused):
                if n == 1:
                    self.control.stop()
            with self.observe(after_metadata=outage) as observed:
                require(self.store.memory.retrieve(room, "gateway") == (memory,), "between_gates_outage_fallback")
            require(observed["metadata_gets"] == 2 and observed["query_posts"] == 0
                    and self.embedder.calls - before == 1, "between_gates_outage_not_exercised")
            self.record("actual_stop_between_metadata_gates", observed)
        finally:
            self.control.start()
        with self.observe() as recovered:
            require(self.store.memory.retrieve(room, "semanticprobe") == (memory,), "between_gates_recovery_missing")
        require(recovered["metadata_gets"] == 2 and recovered["query_posts"] == 1, "recovery_read_shape_changed")

    def run(self):
        base = super().run()
        room = self.new_room()
        allowed = self.save("允许语义来源", room=room)
        excluded = self.save("当前讨论排除", room=room)
        self.save("其他房间排除", room=self.other)
        self.worker.run_once(limit=200)
        correlation = self.records[excluded.memory_id].anchor.source_correlation_id
        with self.observe() as observed:
            require(self.store.memory.retrieve(room, "semanticprobe", exclude_correlation_id=correlation) == (allowed,),
                    "optimized_room_discussion_scope_changed")
        require(observed == {"metadata_gets": 2, "query_posts": 1, "snapshot_selects": [5, 5]}, "optimized_read_shape_changed")
        self.record("optimized_scope_and_http_sql_shape", observed)
        self.races()
        self.schema_boundaries()
        audit = self.audit()
        self.checks.append("optimized_final_two_way_audit_and_drained_outbox")
        base.update(checks=self.checks, final_audit=audit, outbox_status=self.worker.status(),
                    read_boundaries=self.boundaries, synthetic_query_vector_requests=self.embedder.calls,
                    race_scope="real query response then independent Python thread commits SQLite; controlled interleaving, not OS worker kill")
        return base


def probe(*, output_root=RESULTS):
    output = output_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    control = CountedDocker(output / "qdrant-storage")
    report = {"complete": False, "cleanup_complete": False, "default_changed": False,
              "embedding_calls": 0, "agent_calls": 0, "image": IMAGE, "modes": {},
              "read_path_revision": SNAPSHOT_READ_REVISION, "remote_readiness_revision": READINESS_CHECK_REVISION,
              "code_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in PROVENANCE}}
    def checkpoint():
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    checkpoint()
    journey = None
    try:
        control.create()
        control.start()
        report["image_id"], report["container_name"] = control.image_id, control.name
        for mode in MODES:
            print(f"[mode {mode}] existing recovery matrix + optimized read boundaries", flush=True)
            endpoint = QdrantEndpoint(base_url=control.url, collection_prefix="codecrew_optimized_" + mode, timeout_seconds=1)
            backend = QdrantVersionedVectorBackend(endpoint, VectorModel(SyntheticEmbedder.model_fingerprint, 3))
            backend.initialize()
            journey = OptimizedJourney(output / f"{mode}.sqlite3", backend, endpoint, control, lexical_backend=mode)
            report["modes"][mode] = journey.run()
            checkpoint()
        require(control.stops == 14 and control.starts == 15, "actual_stop_start_budget_changed")
        report["matrix_passed"] = True
    except Exception as exc:  # noqa: BLE001 - safe category only, never raw provider or fact text
        report["error_type"] = type(exc).__name__
        if journey is not None:
            report["partial_checks"] = journey.checks
            report["partial_boundaries"] = journey.boundaries
    finally:
        report["actual_stops"], report["actual_starts_including_initial"] = control.stops, control.starts
        try:
            report["cleanup_complete"] = control.cleanup()
        except Exception as exc:  # noqa: BLE001 - ownership failure must not mutate a foreign container
            report["cleanup_error_type"] = type(exc).__name__
        report["complete"] = bool(report.get("matrix_passed") and report["cleanup_complete"])
        checkpoint()
    return output, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    if not args.live:
        print("Plan only: legacy/BM25 in one owned loopback Docker; 14 stop/start pairs, owned index faults and "
              "query-response SQLite writer races; cleanup own container, retain evidence. No settings/cloud/Agent/real chat/default changes.")
        return
    output, report = probe()
    print(f"Results: {output}")
    if not report["complete"]:
        parser.exit(1, "Optimized regression incomplete; inspect safe report\n")
    print("Optimized regression passed for both lexical modes; owned cleanup passed; models 0/0")


if __name__ == "__main__":
    main()
