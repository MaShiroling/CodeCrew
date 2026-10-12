"""Reopen the reviewed persistent target in two fresh, read-only OS processes.

No models, messages, migrations, delivery, repair, daemon or Docker restart.
Cached-vector self-match checks storage/read fencing, NOT new query quality.
Plan opens nothing; --check is local only; --run owns and reaps two children.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from app.chat.dispatch import _closed_open_loop_status_receipt
from app.chat.memory import ChatMemoryStore
from app.chat.qdrant_memory_index import QdrantMemoryVectorIndex
from app.chat.qdrant_vector_sync import QdrantVersionedVectorBackend
from app.chat.store import StandaloneChatStore
from app.chat.vector_outbox import MemoryVectorOutbox
from app.chat.vector_worker_contracts import check_vector_worker_target
from app.storage import SQLiteDatabase
from evals import run_memory_persistent_followup as followup
from evals.memory_persistent_resume import database_snapshot, target_lock
from scripts import recover_memory_live_miswrite as recovery
from scripts.backup_memory_live_database import private_json, readonly
from scripts.run_memory_live_worker import (
    ROOT,
    child_environment,
    cleanup_child,
    load_target,
    require,
)

REVISION = "persistent-restart-v1"
REVIEWED_FOLLOWUP_SHA = "7bbcf51f4fd58353f956540cda4ca343fbd5903a9e9bba7b78f1008fd21f299b"
MAX_REPORT_BYTES = 2_000_000
MAX_CHILD_BYTES = 65536


def read_report(path, directory, expected_sha):
    path = Path(path).resolve(strict=True)
    if path.is_dir():
        path = (path / "report.json").resolve(strict=True)
    require(path.name == "report.json" and path.parent.parent == directory, "restart_report_outside_target")
    with path.open("rb") as stream:
        raw = stream.read(MAX_REPORT_BYTES + 1)
    require(len(raw) <= MAX_REPORT_BYTES and hashlib.sha256(raw).hexdigest() == expected_sha, "restart_report_changed")
    record = json.loads(raw)
    require(isinstance(record, dict), "restart_invalid_report")
    return path, record


def fingerprints():
    return {**followup.fingerprints(), **{p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in (
        "evals/check_memory_persistent_restart.py", "app/storage/sqlite.py", "app/chat/sqlite_vector_backend.py",
        "app/chat/vector_sync_contracts.py", "app/chat/vector_contracts.py", "app/chat/vector_maintenance_contracts.py",
        "scripts/backup_memory_live_database.py")}}


def inspect_followup(target, source):
    """Pinned, already-read answers; retain their pending-review field unchanged."""
    path, record = read_report(source, target.directory / "followup-runs", REVIEWED_FOLLOWUP_SHA)
    require(record.get("revision") == followup.REVISION and record.get("complete") is True
        and record.get("followup_passed") is True and record.get("full_lifecycle_passed") is False
        and record.get("checks_passed") is True and record.get("dispatcher_shutdown_complete") is True
        and record.get("final_state_unchanged") is True and not record.get("error_type")
        and record.get("manifest") == str(target.manifest) and record.get("manifest_sha256") == target.manifest_sha256
        and record.get("code_sha256") == followup.fingerprints()
        and record.get("kimi_starts") == 1 and record.get("embedding_requests") == 0
        and record.get("embedding_blocked_attempts") == 0 and not record.get("proposals") and not record.get("actions")
        and record.get("audit", {}).get("consistent_at_observation") is True, "restart_followup_not_verified")
    parent, original = read_report(record["parent_report"], target.directory / "live-runs", recovery.REVIEWED_SHA)
    require(record.get("parent_sha256") == recovery.REVIEWED_SHA
        and original.get("manifest_sha256") == target.manifest_sha256
        and original.get("database") == str(target.database)
        and original.get("worker_cleanup_complete") is True and original.get("worker_exit") == 0,
        "restart_parent_not_verified")
    require(len(original.get("steps", [])) == 12 and all(entry.get("passed") is True
        and entry.get("step") == followup.STEPS[i][0] and entry.get("turn", {}).get("status") == "succeeded"
        for i, entry in enumerate(original["steps"][:10])), "restart_prefix_not_verified")
    recovery_path, recovered = read_report(record["recovery_report"], target.directory / "recovery-runs", record["recovery_sha256"])
    require(recovered.get("complete") is True and recovered.get("phase") == "recovered_and_audited"
        and recovered.get("memory_id") == str(recovery.BAD_ID)
        and recovered.get("target_id") == target.backend.sync_target_id
        and recovered.get("manifest_sha256") == target.manifest_sha256
        and recovered.get("parent_report_sha256") == recovery.REVIEWED_SHA
        and recovered.get("after") == record.get("before"), "restart_recovery_not_verified")
    require(record.get("historical_kimi_starts") == original["kimi_starts"]
        and record.get("historical_embedding_requests") == original["embedding_requests"]
        and record.get("cumulative_kimi_starts") == original["kimi_starts"] + 1
        and record.get("cumulative_embedding_requests") == original["embedding_requests"], "restart_budget_ledger_changed")
    require(len(record.get("steps", [])) == 2 and all(entry.get("passed") is True
        and entry.get("step") == followup.CASES[i][0] and entry.get("turn", {}).get("status") == "succeeded"
        and entry.get("context") == [] for i, entry in enumerate(record["steps"])), "restart_followup_steps_changed")
    snapshot = database_snapshot(target.database)
    require(snapshot == record.get("after"), "restart_database_changed")
    evidence = {str(path): REVIEWED_FOLLOWUP_SHA, str(parent): recovery.REVIEWED_SHA,
        str(recovery_path): record["recovery_sha256"], str(target.manifest): target.manifest_sha256}
    # Also preserve the original V1 prefix when present; never re-score its vector miss.
    if original.get("resume_from"):
        legacy, _ = read_report(original["resume_from"], target.directory / "live-runs", original["parent_report_sha256"])
        evidence[str(legacy)] = original["parent_report_sha256"]
    return {"followup": path, "record": record, "original": original, "before": snapshot, "evidence": evidence}


class ReadOnlyDatabase(SQLiteDatabase):
    """Production read APIs over mode=ro; every write entry fails before SQL."""
    def _open_connection(self):
        conn = readonly(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def transaction(self, *, immediate=True):
        raise RuntimeError("restart_write_forbidden")
        yield  # pragma: no cover - preserve contextmanager interface


class CachedVectorReplay:
    """Not an embedding model: exactly one known text's persisted vector."""
    def __init__(self, mutation, text):
        self.model_fingerprint = mutation.model.fingerprint
        self.vector, self.text, self.calls = mutation.record.vector, text, 0

    def embed(self, texts):
        require(tuple(texts) == (self.text,), "restart_new_query_vector_forbidden")
        self.calls += 1
        return (self.vector,)


def canonical_checks(target, state):
    database = ReadOnlyDatabase(target.database)
    memory_store = ChatMemoryStore(database, lexical_backend="bm25")
    original = state["original"]
    rooms = {key: UUID(value) for key, value in original["rooms"].items()}
    expected = {}
    with database.connect() as conn:
        conn.execute("BEGIN")
        rows = conn.execute("SELECT * FROM standalone_chat_memories").fetchall()
        require(len(rows) == 5, "restart_memory_count_changed")
        by_source = {r["source_message_id"]: ChatMemoryStore._from_row(r) for r in rows}
        require(len(by_source) == 5, "restart_duplicate_memory_source")
        for i in (0, 2, 4, 7):
            expected[i] = by_source.get(original["steps"][i]["source"])
            proposals = [p for p in original.get("write_proposals", [])
                if p.get("source") == original["steps"][i]["source"] and p.get("accepted")]
            require(expected[i] is not None and expected[i].room_id == rooms[followup.STEPS[i][1]]
                and expected[i].kind == followup.STEPS[i][3] and len(proposals) == 1
                and str(expected[i].memory_id) == proposals[0]["accepted"]
                and expected[i].content == proposals[0]["proposal"]["content"],
                "restart_canonical_fact_changed")
        old, active, dev_todo, personal_todo = (expected[i] for i in (0, 2, 4, 7))
        require(old.status == "retired" and old.retired_reason == "replaced" and active.status == "active"
            and active.replaces_memory_id == old.memory_id, "restart_replacement_changed")
        for todo, status, step in ((dev_todo, "completed", 6), (personal_todo, "cancelled", 9)):
            require(todo.status == "retired" and todo.open_loop_state == status
                and str(todo.closed_by_message_id) == original["steps"][step]["source"], "restart_closure_changed")
        bad = next((ChatMemoryStore._from_row(r) for r in rows if r["memory_id"] == str(recovery.BAD_ID)), None)
        require(bad is not None and bad.status == "retired" and bad.retired_reason == "manual"
            and bad.room_id == rooms["personal"] and str(bad.source_message_id) == original["steps"][11]["source"],
            "restart_miswrite_resurrected")
        for table in ("standalone_chat_memory_embeddings", "standalone_chat_memory_fts_documents"):
            require({r[0] for r in conn.execute("SELECT memory_id FROM " + table)} == {str(active.memory_id)},
                "restart_retired_derivative_present")
        require(memory_store.fts_index._documents_current(conn), "restart_fts_projection_changed")
        require(memory_store.fts_index.status()["usable"], "restart_fts_not_ready")
        all_memories = tuple(ChatMemoryStore._from_row(r) for r in rows)
        worker = MemoryVectorOutbox(database, target.backend)
        revisions = conn.execute("SELECT * FROM standalone_chat_vector_revisions ORDER BY memory_id").fetchall()
        require(len(revisions) == 5, "restart_revision_count_changed")
        desired = worker.desired_many_in_snapshot(conn, revisions)
        mutations = tuple(desired[r["memory_id"]][0] for r in revisions)
        require(all(waiting is None for _, waiting in desired.values()), "restart_cache_missing_or_stale")
        require({m.memory_id for m in mutations if m.record is not None} == {active.memory_id}, "restart_unexpected_active_vector")
    require(memory_store.list_active(rooms["dev"]) == (active,) and memory_store.list_active(rooms["personal"]) == (),
        "restart_active_membership_changed")
    require(memory_store.list_closed_open_loops(rooms["dev"]) == (dev_todo,)
        and memory_store.list_closed_open_loops(rooms["personal"]) == (personal_todo,), "restart_closed_membership_changed")
    lineage = memory_store.lineage(rooms["dev"], active.memory_id)
    require(lineage == ((active, old), False), "restart_lineage_changed")
    ids = lambda memories: [str(m.memory_id) for m in memories]
    lexical = ids(memory_store.retrieve(rooms["dev"], "API 网关读超时", limit=3))
    require(lexical == [str(active.memory_id)], "restart_bm25_recall_failed")
    require(not memory_store.retrieve(rooms["dev"], dev_todo.content)
        and not memory_store.retrieve(rooms["personal"], personal_todo.content)
        and not memory_store.retrieve(rooms["personal"], "API 网关读超时"), "restart_closed_or_room_leak")
    store = StandaloneChatStore(database, lexical_backend="bm25")
    receipt = _closed_open_loop_status_receipt(store, store.get_room(rooms["personal"]),
        store.get_message(UUID(state["record"]["steps"][0]["source"])))
    require(receipt == state["record"]["steps"][0]["turn"]["reply"], "restart_cancelled_receipt_changed")
    require(not any(worker.status().get(k, 0) for k in ("pending", "processing", "waiting", "dead")), "restart_backlog_remaining")
    return database, memory_store, all_memories, active, mutations, rooms, {
        "bm25_ids": lexical, "active_count": 1, "retired_count": 4, "closed_completed": 1, "closed_cancelled": 1,
        "lineage_preserved": True, "cancelled_receipt_preserved": True, "source_history_preserved": True,
        "outbox_status": worker.status(), "cache_and_fts_count": 1}


def observe(target, state):
    database, memory_store, memories, active, mutations, rooms, result = canonical_checks(target, state)
    check_vector_worker_target(target.database, target.backend)  # Metadata reads only, no allocation.
    inspections = target.backend.inspect_mutations(mutations)
    require(len(inspections) == len(mutations) and all(o.memory_id == m.memory_id and o.code == "current"
        and o.remote_revision == m.revision for o, m in zip(inspections, mutations, strict=True)), "restart_remote_drift")
    expected = {QdrantVersionedVectorBackend.mutation_point_id(m): (m.room_id, m.memory_id) for m in mutations}
    seen, cursor, complete = set(), None, False
    for _ in range(8):
        page = target.backend.scan_identities(cursor, limit=16)
        for item in page.points:
            require(item.point_id not in seen and expected.get(item.point_id) == (item.room_id, item.memory_id),
                "restart_remote_orphan_or_scope_conflict")
            seen.add(item.point_id)
        if page.next_cursor is None:
            complete = True
            break
        require(page.next_cursor != cursor, "restart_remote_cursor_stalled")
        cursor = page.next_cursor
    require(complete and seen == set(expected), "restart_remote_scan_incomplete")
    mutation = next(m for m in mutations if m.record is not None)
    replay = CachedVectorReplay(mutation, active.content)
    index = QdrantMemoryVectorIndex(database, replay, target.backend.endpoint, backend_factory=lambda model: target.backend)
    hits = index.search_scoped(active.content, memories, room_id=rooms["dev"])
    require(len(hits) == 1 and hits[0][0] == active.memory_id and hits[0][1] >= .99, "restart_cached_vector_self_match_failed")
    memory_store.vector_index = index
    require(tuple(m.memory_id for m in memory_store.retrieve(rooms["dev"], active.content)) == (active.memory_id,),
        "restart_cached_hybrid_failed")
    require(index.search_scoped(active.content, memories, room_id=rooms["personal"]) == (), "restart_vector_room_leak")
    require(index.search_scoped(active.content, memories, room_id=rooms["dev"],
        exclude_correlation_id=mutation.record.anchor.source_correlation_id) == (), "restart_vector_discussion_leak")
    require(replay.calls == 2, "restart_unexpected_vector_replay_count")
    require(database_snapshot(target.database) == state["before"], "restart_database_changed_during_observation")
    result.update(remote_current=len(inspections), remote_scanned=len(seen), remote_tombstones=4,
        audit_consistent_at_observation=True, cached_vector_self_match=True, cached_vector_replays=replay.calls,
        cached_hybrid_recall=True, vector_room_and_discussion_isolation=True)
    return result


def evidence_unchanged(state):
    return all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == sha for path, sha in state["evidence"].items())


def run_restart(target, source, *, launcher=subprocess.Popen):
    with target_lock(target.directory):
        state = inspect_followup(target, source)
        canonical_checks(target, state)  # Fail locally before child/report allocation.
        output = target.directory / "restart-runs" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        report = {"complete": False, "restart_passed": False, "revision": REVISION,
            "acceptance_basis": "reviewed_prefix_plus_recovery_plus_followup_plus_restart_not_fresh_12_step_rerun",
            "followup_answer_review": "assistant_reviewed_fixed_answers_old_pending_human_review_field_unchanged",
            "manifest": str(target.manifest), "database": str(target.database), "target_id": target.backend.sync_target_id,
            "followup_report": str(state["followup"]), "evidence_sha256": state["evidence"],
            "before": state["before"], "code_sha256": fingerprints(), "processes": [],
            "agent_calls": 0, "embedding_api_calls": 0, "cumulative_kimi_starts": state["record"]["cumulative_kimi_starts"],
            "cumulative_embedding_requests": state["record"]["cumulative_embedding_requests"],
            "vector_probe": "persisted_document_vector_self_match_not_new_query_embedding",
            "docker_restarted": False, "chat_service_restarted": False, "background_worker_started": False}
        def checkpoint():
            temp = output / (uuid4().hex + ".tmp.json")
            private_json(temp, report)
            temp.replace(output / "report.json")
        children, cleaned = [], []
        checkpoint()
        try:
            for number in (1, 2):
                print(f"[restart] fresh reader {number}/2", flush=True)
                nonce = uuid4().hex
                stdout, stderr = output / f"child-{number}.json", output / f"child-{number}.stderr"
                for path in (stdout, stderr):
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.close(fd)
                with stdout.open("w") as out, stderr.open("w") as err:
                    proc = launcher([sys.executable, "-m", "evals.check_memory_persistent_restart", "--child",
                        "--manifest", str(target.manifest), "--followup", str(state["followup"]), "--nonce", nonce],
                        cwd=output, env={**child_environment(), "PYTHONDONTWRITEBYTECODE": "1"},
                        stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
                    children.append(proc)
                    require(type(proc.pid) is int and proc.pid > 0 and proc.pid != os.getpid(), "restart_owned_child_required")
                    entry = {"pid": proc.pid, "returncode": None, "nonce": nonce}
                    report["processes"].append(entry)
                    checkpoint()
                    try:
                        proc.wait(timeout=45)
                    finally:
                        cleaned.append(cleanup_child(proc))
                        entry["returncode"] = proc.returncode
                require(stdout.stat().st_size <= MAX_CHILD_BYTES and stderr.stat().st_size == 0, "restart_unexpected_child_output")
                entry["observation"] = json.loads(stdout.read_text())
                observation = entry["observation"]
                require(proc.returncode == 0 and observation.get("complete") is True and observation.get("pid") == proc.pid
                    and observation.get("nonce") == nonce and observation.get("database") == str(target.database)
                    and observation.get("target_id") == target.backend.sync_target_id
                    and observation.get("before") == state["before"] == observation.get("after")
                    and observation.get("code_sha256") == report["code_sha256"]
                    and observation.get("evidence_sha256") == state["evidence"]
                    and observation.get("agent_calls") == observation.get("embedding_api_calls") == 0,
                    "restart_child_failed_or_changed")
                checkpoint()
            require(len({p["pid"] for p in report["processes"]}) == 2
                and report["processes"][0]["observation"]["checks"] == report["processes"][1]["observation"]["checks"],
                "restart_fresh_observations_differ")
            report["checks_passed"] = True
        except Exception as exc:  # noqa: BLE001 - never emit private SQL/provider/credential bodies
            report["error_type"] = type(exc).__name__
            report["failure_category"] = "restart_verification_failed"
        finally:
            for proc in children:
                try:
                    cleaned.append(cleanup_child(proc))
                except Exception as exc:  # noqa: BLE001
                    cleaned.append(False)
                    report["cleanup_error_type"] = type(exc).__name__
            report["launched_readers"] = len(children)
            report["children_cleanup_complete"] = all(cleaned) and all(p.poll() is not None for p in children)
            try:
                report["after"] = database_snapshot(target.database)
                report["final_state_unchanged"] = report["after"] == state["before"] and evidence_unchanged(state) and fingerprints() == report["code_sha256"]
            except Exception as exc:  # noqa: BLE001
                report["final_state_unchanged"] = False
                report["final_check_error_type"] = type(exc).__name__
            report["complete"] = bool(report.get("checks_passed") and report["children_cleanup_complete"]
                and report["final_state_unchanged"] and not report.get("error_type"))
            report["restart_passed"] = report["complete"]
            checkpoint()
        return output, report


def child(manifest, source, nonce):
    require(isinstance(nonce, str) and len(nonce) == 32 and UUID(hex=nonce).hex == nonce, "restart_child_nonce_required")
    target = load_target(manifest)
    state = inspect_followup(target, source)
    report = {"complete": False, "pid": os.getpid(), "nonce": nonce, "database": str(target.database),
        "target_id": target.backend.sync_target_id, "before": state["before"], "code_sha256": fingerprints(),
        "evidence_sha256": state["evidence"], "agent_calls": 0, "embedding_api_calls": 0}
    try:
        report["checks"] = observe(target, state)
        report["after"] = database_snapshot(target.database)
        require(report["after"] == state["before"] and evidence_unchanged(state) and fingerprints() == report["code_sha256"],
            "restart_child_final_state_changed")
        report["complete"] = True
    except Exception as exc:  # noqa: BLE001
        report["error_type"] = type(exc).__name__
        report["failure_category"] = "restart_observation_failed"
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["complete"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="local-only read preflight")
    modes.add_argument("--run", action="store_true", help="two fresh readers with real Qdrant reads, zero model calls")
    modes.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--followup", type=Path)
    parser.add_argument("--nonce", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.nonce is not None and not args.child:
        parser.error("nonce is child-only")
    if not (args.check or args.run or args.child):
        print("Plan only: two fresh OS readers on reviewed same SQLite/Qdrant; zero models, messages, writes, delivery, repair or Docker/service restart.")
        return
    if args.manifest is None or args.followup is None:
        parser.error("explicit --manifest and --followup required")
    try:
        if args.child:
            parser.exit(child(args.manifest, args.followup, args.nonce))
        target = load_target(args.manifest)
        if args.check:
            state = inspect_followup(target, args.followup)
            canonical_checks(target, state)
            require(database_snapshot(target.database) == state["before"], "restart_preflight_database_changed")
            print("Reviewed follow-up and canonical state verified; local read-only; models/processes/remote=0/0/0")
            return
        output, report = run_restart(target, args.followup)
        print("Results: " + str(output / "report.json"))
        if not report["complete"]:
            parser.exit(1, "Restart verification incomplete; evidence retained; no repair/retry\n")
        print("Same-database fresh-process restart passed; retired memories not resurrected; audit consistent; models=0/0; original reports unchanged")
    except Exception as exc:  # noqa: BLE001
        parser.exit(1, "Restart refused: " + type(exc).__name__ + "\n")


if __name__ == "__main__":
    main()
