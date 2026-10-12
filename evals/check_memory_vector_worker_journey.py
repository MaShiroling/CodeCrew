"""Opt-in isolated writer/cache/watch/Qdrant/recall journey; no paid models.

Deterministic Agent proposals/actions enter the production capture boundary, not
an LLM or reply dispatcher. Real local cache writes, owned child CLI and Qdrant.
Plan mode opens nothing. Reuses stage-4 ownership guards without modifying them.
"""

import argparse
import hashlib
import json
import platform
import sys
from datetime import datetime, timezone
from uuid import uuid4

from app.chat.dispatch import chat_context
from app.chat.memory import AgentMemoryAction, AgentMemoryProposal, lexical_memory_ranking
from app.chat.qdrant_vector_backend import QdrantEndpoint
from app.chat.qdrant_vector_sync import QdrantVersionedVectorBackend
from app.chat.vector_contracts import VectorModel
from evals.benchmark_memory_qdrant import finish_audit
from evals.check_memory_qdrant_faults import (
    IMAGE,
    ROOT,
    FaultJourney,
    OwnedDocker,
    SyntheticEmbedder,
    require,
)
from evals.check_memory_vector_worker_processes import PROVENANCE as PROCESS_PROVENANCE
from evals.check_memory_vector_worker_processes import ProcessJourney, wait_until

RESULTS = ROOT / "evals/results/memory-vector-worker-journey-v1"
PROVENANCE = (*PROCESS_PROVENANCE, "evals/check_memory_vector_worker_journey.py",
              "app/chat/qdrant_memory_index.py", "app/chat/fts_memory.py", "app/chat/dispatch.py")
QUERY = "semanticprobe"


class JourneyEmbedder(SyntheticEmbedder):
    """Synthetic-only failure toggle, never replaces a production provider."""
    fail = False

    def embed(self, texts):
        if self.fail:
            raise RuntimeError("synthetic_embedding_unavailable")
        return super().embed(texts)


class WriterJourney(FaultJourney):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.embedder = JourneyEmbedder()
        self.index.embedder = self.embedder
        # Before the first embedding establishes dimensions, the production
        # index uses a 2D eligibility-only model (never sent to remote storage).
        # Do not force the prepared 3D backend into that local snapshot.
        endpoint = args[2]
        self.index._backend = lambda model: (self.backend if model == self.backend.model
                                            else QdrantVersionedVectorBackend(endpoint, model))
        self.checks = []
        self.closed_loops = []

    def write(self, text, *, room=None, kind="decision", replaces=None):
        room = room or self.room
        source = self.service.post_message(room, content="@白金 " + text,
            idempotency_key=uuid4(), reply_to=None).message
        proposal = AgentMemoryProposal(kind=kind, content=text, replaces_memory_id=replaces)
        memory = self.store.memory.capture_agent_proposal(room, source.message_id, proposal)
        require(memory is not None and memory.origin == "agent_auto", "automatic_proposal_rejected")
        return memory, source, proposal

    def fingerprint(self):
        # Current durable delivery/cache state only: no lease tokens or text.
        with self.database.connect() as conn:
            conn.execute("BEGIN")
            return tuple(tuple(row) for row in conn.execute(
                "SELECT memory_id,revision,status,attempts FROM standalone_chat_vector_outbox "
                "WHERE target_id=? ORDER BY memory_id,revision", (self.worker.target_id,))), tuple(
                tuple(row) for row in conn.execute(
                    "SELECT memory_id,content_hash,model_fingerprint FROM standalone_chat_memory_embeddings ORDER BY memory_id"))

    def semantic(self, room, expected, *, exclude=None):
        memories = self.store.memory.list_active(room)
        # Prove no lexical fallback could make this a false vector success.
        require(not lexical_memory_ranking(memories, QUERY), "semantic_probe_has_legacy_match")
        if self.lexical_backend == "bm25":
            with self.database.connect() as conn:
                conn.execute("BEGIN")
                require(self.store.memory.fts_index.search(conn, room, QUERY,
                    memory_ids=tuple(m.memory_id for m in memories),
                    exclude_correlation_id=exclude) == (), "semantic_probe_has_bm25_match")
        wanted = {m.memory_id for m in expected}
        hits = self.index.search_scoped(QUERY, memories, room_id=room, exclude_correlation_id=exclude)
        require({mid for mid, score in hits if score >= .65} == wanted, "pure_vector_recall_mismatch")
        recall = self.store.memory.retrieve(room, QUERY, exclude_correlation_id=exclude)
        require({m.memory_id for m in recall} == wanted, "hybrid_recall_mismatch")

    def context(self, room, expected):
        stored = self.service.post_message(room, content="@月见 " + QUERY, idempotency_key=uuid4(), reply_to=None)
        frame = json.loads(chat_context(self.store, stored, self.store.get_room(room)))
        require({m["memory_id"] for m in frame["room_memories"]} == {str(m.memory_id) for m in expected},
                "injected_context_mismatch")

    def remote(self, memory, *, tombstone=False):
        desired = self.desired(memory)
        require((desired.record is None) == tombstone, "canonical_tombstone_mismatch")
        observed = self.backend.inspect_mutations((desired,))[0]
        require(observed.code == "current" and observed.remote_revision == desired.revision,
                "remote_revision_or_tombstone_mismatch")

    def lifecycle(self, delivered, waiting):
        """Drivers are real-child waits in live mode; offline controls are tests only."""
        memory, source, proposal = self.write("API 网关读超时统一为 8 秒。")
        delivered(memory)
        self.remote(memory)
        self.semantic(self.room, (memory,))
        self.context(self.room, (memory,))
        before, calls = self.fingerprint(), self.embedder.calls
        replay = self.store.memory.capture_agent_proposal(self.room, source.message_id, proposal)
        require(replay == memory and self.fingerprint() == before and self.embedder.calls == calls,
                "duplicate_proposal_created_cache_or_event")
        self.checks.append("automatic_create_cache_delivery_semantic_context_idempotence")

        updated, _, _ = self.write("API 网关读超时改为 12 秒。", kind="correction", replaces=memory.memory_id)
        delivered(memory)
        delivered(updated)
        self.remote(memory, tombstone=True)
        self.remote(updated)
        require(updated.replaces_memory_id == memory.memory_id, "replacement_lineage_missing")
        self.semantic(self.room, (updated,))
        self.context(self.room, (updated,))
        self.checks.append("replacement_old_tombstone_latest_only_context")

        foreign, _, _ = self.write("其他房间的部署决定使用队列。", room=self.other)
        delivered(foreign)
        self.semantic(self.room, (updated,))
        self.semantic(self.room, (), exclude=self.store.get_message(updated.source_message_id).message.correlation_id)
        # Query the exact source turn: context excludes its own discussion.
        current = self.store.get_message(updated.source_message_id)
        require(json.loads(chat_context(self.store, current, self.store.get_room(self.room)))["room_memories"] == [],
                "same_discussion_context_leak")
        self.checks.append("other_room_and_same_discussion_isolation")

        for action, text, evidence in (
            ("complete", "待办：完成 gateway loadtest 压测。", "gateway loadtest 压测已经完成。"),
            ("cancel", "待办：预约 dentist checkup 牙科检查。", "取消 dentist checkup 牙科检查。"),
        ):
            room = self.service.create_room(title="synthetic " + action, idempotency_key=uuid4()).room_id
            loop, _, _ = self.write(text, room=room, kind="open_loop")
            delivered(loop)
            self.semantic(room, (loop,))
            self.context(room, (loop,))
            closure = self.service.post_message(room, content="@白金 " + evidence, idempotency_key=uuid4(), reply_to=None).message
            proposed = AgentMemoryAction(action=action, memory_id=loop.memory_id, evidence=evidence)
            closed = self.store.memory.capture_agent_action(room, closure.message_id, proposed)
            expected_state = "completed" if action == "complete" else "cancelled"
            require(closed is not None and closed.status == "retired" and closed.open_loop_state == expected_state
                    and closed.closed_by_message_id == closure.message_id and closed.source_message_id == loop.source_message_id,
                    "grounded_closure_or_history_mismatch")
            delivered(closed)
            self.remote(closed, tombstone=True)
            self.semantic(room, ())
            self.context(room, ())
            require(closed in self.store.memory.list_closed_open_loops(room), "closed_history_missing")
            self.closed_loops.append(closed)
            before = self.fingerprint()
            require(self.store.memory.capture_agent_action(room, closure.message_id, proposed) == closed
                    and self.fingerprint() == before, "closure_replay_created_event")
            self.checks.append("automatic_" + action + "_tombstone_history_recall_context_absent")

        self.embedder.fail = True
        missing, _, _ = self.write("部署流水线使用 Jenkins。")
        waiting(missing)
        self.embedder.fail = False
        before = self.fingerprint()
        self.semantic(self.room, (updated,))
        self.context(self.room, (updated,))
        require(self.fingerprint() == before, "query_backfilled_cache_or_delivered_event")
        self.index.index((missing,))  # Explicit writer cache retry, not a query or worker embedding.
        delivered(missing)
        self.remote(missing)
        self.semantic(self.room, (updated, missing))
        self.context(self.room, (updated, missing))
        self.checks.append("missing_cache_waiting_no_query_backfill_explicit_writer_retry")
        return updated, missing

    def closed_after_restart(self):
        for closed in self.closed_loops:
            self.remote(closed, tombstone=True)
            self.semantic(closed.room_id, ())
            self.context(closed.room_id, ())
            require(closed in self.store.memory.list_closed_open_loops(closed.room_id), "restart_lost_closed_history")


class LiveJourney(ProcessJourney):
    def run(self):
        results = []
        for lexical in ("legacy", "bm25"):
            directory = self.output / lexical
            directory.mkdir(exist_ok=False)
            endpoint = QdrantEndpoint(base_url=self.control.url,
                collection_prefix="codecrew_worker_journey_" + lexical, timeout_seconds=5)
            backend = QdrantVersionedVectorBackend(endpoint, VectorModel(SyntheticEmbedder.model_fingerprint, 3))
            backend.initialize()
            journey = WriterJourney(directory / "chat.sqlite3", backend, endpoint, self.control, lexical_backend=lexical)
            self.journeys.append((lexical, journey))
            child = self.spawn(journey, endpoint, "worker")
            wait_until(lambda child=child: child.marker("ready"))
            def waiting(memory, child=child, journey=journey):
                def observed():
                    require(child.alive(), "worker_exited_before_waiting")
                    return self.row(journey, memory)["status"] == "waiting"
                wait_until(observed)
            updated, missing = journey.lifecycle(
                lambda m, journey=journey, child=child: self.wait_delivered(journey, m, child), waiting)
            first = self.finish_child(child)
            # Queue while worker is stopped, then use a different OS process.
            offline, _, _ = journey.write("日志留存固定为 14 天。")
            require(self.row(journey, offline)["attempts"] == 0, "parent_delivered_stopped_worker_event")
            before = journey.fingerprint()
            journey.restart_store()
            require(journey.fingerprint() == before, "store_restart_changed_cache_or_delivery")
            restarted = self.spawn(journey, endpoint, "restart")
            self.wait_delivered(journey, offline, restarted)
            require(child.proc.pid != restarted.proc.pid, "restart_same_pid")
            journey.semantic(journey.room, (updated, missing, offline))
            journey.context(journey.room, (updated, missing, offline))
            journey.closed_after_restart()
            journey.checks.append("store_worker_restart_queued_write_no_resurrection")
            last = self.finish_child(restarted)
            audit = finish_audit(journey.worker)
            status = journey.worker.status()
            require(not any(status.get(s, 0) for s in ("pending", "processing", "waiting", "dead")), "journey_backlog_remaining")
            require(last["report"]["diagnostics"]["last_delivered_at_unix"] is not None, "delivery_time_missing")
            result = {"lexical": lexical, "checks": journey.checks, "process": first, "restart": last,
                      "final_audit": audit, "outbox_status": status, "synthetic_embed_texts": journey.embedder.calls}
            results.append(result)
            self.record(lexical + "_automatic_writer_background_recall", checks=journey.checks)
        return {"arms": results, "checks": self.checks}


def run_suite(*, output_root=RESULTS):
    require(sys.platform != "win32", "POSIX_signals_required")
    output = output_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    control = OwnedDocker(output / "qdrant-storage")
    journey = LiveJourney(output, control)
    report = {"complete": False, "image": IMAGE, "python": platform.python_version(), "embedding_calls": 0,
              "agent_calls": 0, "default_changed": False, "writer_boundary": "deterministic_proposal_action_capture_not_llm_or_dispatch",
              "code_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in PROVENANCE}}
    def checkpoint():
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    checkpoint()
    try:
        control.create()
        control.start()
        report.update(image_id=control.image_id, container_name=control.name)
        report.update(journey.run())
        require(all(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == sha for p, sha in report["code_sha256"].items()),
                "code_changed_during_journey")
        report["matrix_passed"] = True
    except Exception as exc:  # noqa: BLE001 - no private/provider error text
        report["error_type"] = type(exc).__name__
        report["completed_checks"] = journey.checks
        report["partial_arms"] = [{"lexical": name, "checks": j.checks} for name, j in journey.journeys]
    finally:
        for key, cleanup in (("children_cleanup_complete", journey.cleanup), ("container_cleanup_complete", control.cleanup)):
            try:
                report[key] = cleanup()
            except Exception as exc:  # noqa: BLE001 - preserve ownership fence
                report[key + "_error_type"] = type(exc).__name__
        report["cleanup_complete"] = bool(report.get("children_cleanup_complete") and report.get("container_cleanup_complete"))
        report["complete"] = bool(report.get("matrix_passed") and report["cleanup_complete"])
        checkpoint()
    return output, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    if not args.live:
        print("Plan only: deterministic proposal/action capture -> automatic synthetic cache -> owned watch CLI "
              "-> isolated Qdrant -> pure vector/hybrid/context; legacy + BM25, replace/complete/cancel/restart. "
              "No .env/keys/paid embeddings/LLMs/reply dispatcher/shared 6333/real chat/default changes.")
        return
    output, report = run_suite()
    print(f"Results: {output}")
    if not report["complete"]:
        parser.exit(1, "Journey incomplete; not scored as memory misses\n")
    print("Automatic writer/background retrieval journey passed; owned cleanup complete; models 0/0")


if __name__ == "__main__":
    main()
