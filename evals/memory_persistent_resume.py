"""Read-only, fail-closed replay evidence validation; no models or remote calls.

V2 checkpoints contain a durable SQLite fingerprint. One explicitly reviewed V1
report can migrate to hybrid adjudication; its original bytes stay immutable.
Local advisory locking coordinates these runners, not arbitrary DB writers.
"""

import copy
import fcntl
import hashlib
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.chat.memory import ChatMemoryStore
from scripts.backup_memory_live_database import readonly

REVISION = "persistent-hybrid-v3"
MIN_SIMILARITY = .65
LEGACY_RUNNER_SHA = "2114b715aecf0c1798d3314f3d5ec49e6ebed8d3c5d2e2889c85b4f71c269ea0"
LEGACY_REPORT_SHA = "6cbbbc5f88492de68e9919d8a60acc2e3d2540052f3317ddb0ebdb13d337aad5"
RUNNER = "evals/run_memory_persistent_live.py"
RESUME_MODULE = "evals/memory_persistent_resume.py"
QUERY_TARGETS = {"recall_decision": "create_decision", "recall_replacement": "replace_decision",
                 "recall_dev_todo": "create_dev_todo", "recall_personal_todo": "create_personal_todo"}


class ResumeError(ValueError):
    """Fixed categories only, never user fact/SQL/provider text."""


def check(value, category):
    if not value:
        raise ResumeError(category)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), default=lambda b: {"bytes_hex": bytes(b).hex()}).encode()).hexdigest()


def _snapshot(conn):
    tables = sorted(row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if row[0] == "schema_migrations" or row[0].startswith("standalone_chat_"))
    # Include cache/revision/outbox/telemetry/FTS/maintenance, not just active facts.
    body = {t: sorted((dict(row) for row in conn.execute('SELECT * FROM "' + t.replace('"', '""') + '"')),
              key=digest) for t in tables}
    schema = [dict(row) for row in conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")
              if row["tbl_name"] in tables]
    return {"sha256": digest({"schema": schema, "rows": body}), "counts": {t: len(rows) for t, rows in body.items()}}


def database_snapshot(path):
    with closing(readonly(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        return _snapshot(conn)


@contextmanager
def target_lock(directory):
    # O_NOFOLLOW prevents redirecting this lock file into unrelated user files.
    fd = os.open(Path(directory) / ".persistent-live.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ResumeError("target_runner_already_locked") from None
        yield
    finally:
        os.close(fd)  # No PID-file signaling or lock-file deletion.


def hybrid_query_check(name, reply, contexts, memories):
    check(isinstance(reply, str) and reply.strip(), "saved_reply_missing")
    if name in QUERY_TARGETS:
        memory = memories[QUERY_TARGETS[name]]
        check(str(memory.memory_id) in {m["memory_id"] for m in contexts}, "actual_prompt_recall_missing")
        if name in {"recall_decision", "recall_replacement"}:
            number = "8" if name == "recall_decision" else "12"
            check(number in reply and "秒" in reply, "answer_timeout_value_missing")
    else:
        check(not contexts, "closed_or_other_room_context_leak")
        if name == "closed_personal_query":
            memory = memories["create_personal_todo"]
            check(memory.kind == "open_loop" and memory.status == "retired" and memory.open_loop_state == "cancelled",
                  "closed_state_not_confirmed")
            expected = (f"待办「{memory.content}」在记忆记录中标记为已取消，"
                "不再属于未完成待办；这不独立证明现实中的执行结果。"
                "原始消息仍保留，本轮没有修改待办。")
            check(reply == expected, "closed_state_receipt_missing")
        if name == "other_room_isolation":
            check(not any(n in reply for n in ("8 秒", "8秒", "12 秒", "12秒")), "other_room_decision_in_answer")


@dataclass(frozen=True)
class ResumeState:
    report_path: Path
    report_sha256: str
    entries: list
    rooms: dict
    memories: dict
    kimi_used: int
    embedding_used: int
    snapshot: dict
    legacy: bool
    proposals: list
    actions: list

    def summary(self):
        return {"validated": True, "resume_from": str(self.report_path), "parent_sha256": self.report_sha256,
                "completed_hybrid_steps": len(self.entries), "next_step": len(self.entries) + 1,
                "kimi_used": self.kimi_used, "embedding_used": self.embedding_used,
                "kimi_remaining": 12-self.kimi_used, "embedding_remaining": 40-self.embedding_used,
                "legacy_re_adjudication": self.legacy, "new_model_calls": 0}


def validate_resume(target, source, steps, current_code, *, legacy_report_sha=LEGACY_REPORT_SHA):
    path = Path(source).resolve(strict=True)
    if path.is_dir():
        path = (path / "report.json").resolve(strict=True)
    check(path.name == "report.json" and path.parent.parent == target.directory / "live-runs",
          "resume_report_outside_target")
    with path.open("rb") as stream:
        raw = stream.read(2_000_001)
    check(len(raw) <= 2_000_000, "resume_report_unbounded")
    original = json.loads(raw)
    check(isinstance(original, dict), "resume_report_invalid")
    report_sha = hashlib.sha256(raw).hexdigest()
    check(original.get("manifest") == str(target.manifest) and original.get("manifest_sha256") == target.manifest_sha256
          and original.get("database") == str(target.database), "resume_target_changed")
    check(original.get("complete") is False and original.get("smoke") is False, "resume_requires_incomplete_full_run")
    check(original.get("worker_cleanup_complete") is True and original.get("worker_exit") == 0
          and original.get("worker_final", {}).get("reason") == "stopped", "previous_worker_not_cleanly_stopped")
    check(original.get("limits") == {"kimi_starts": 12, "embedding_requests": 40, "kimi_steps_per_turn": 3}, "resume_budget_policy_changed")
    used, embedded = original.get("kimi_starts"), original.get("embedding_requests")
    check(type(used) is int and 0 <= used < 12 and type(embedded) is int and 0 <= embedded < 40
          and original.get("embedding_errors") == 0, "resume_budget_exhausted_or_unknown")
    legacy = original.get("evaluator_revision") is None
    previous_code = original.get("code_sha256")
    check(isinstance(previous_code, dict), "resume_code_evidence_missing")
    check(set(previous_code) == set(current_code) - ({RESUME_MODULE} if legacy else set()),
          "resume_code_evidence_changed")
    if legacy:
        check(report_sha == legacy_report_sha and previous_code.get(RUNNER) == LEGACY_RUNNER_SHA
              and original.get("failure_category") == "direct_semantic_target_missing", "unreviewed_legacy_report")
    else:
        check(original.get("evaluator_revision") == REVISION and original.get("steps_sha256") == digest(steps)
              and original.get("min_similarity") == MIN_SIMILARITY, "resume_evaluation_policy_changed")
    for name, sha in current_code.items():
        if legacy and name in (RUNNER, RESUME_MODULE):
            continue  # Exactly reviewed evaluator migration; production files must match.
        check(previous_code.get(name) == sha, "resume_production_or_evaluator_code_changed")
    entries = copy.deepcopy(original.get("steps"))
    check(isinstance(entries, list) and 0 < len(entries) < len(steps), "resume_prefix_missing_or_complete")
    if legacy:
        check(len(entries) == 6 and entries[-1].get("step") == "recall_dev_todo"
              and entries[-1].get("passed") is False, "unsupported_legacy_checkpoint")
    rooms = original.get("rooms")
    check(isinstance(rooms, dict) and set(rooms) == {s[1] for s in steps[:len(entries)]}, "resume_room_evidence_changed")
    with closing(readonly(target.database)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        snapshot = _snapshot(conn)
        if not legacy:
            check(original.get("database_snapshot") == snapshot, "resume_database_changed")
        mrows = {row["memory_id"]: row for row in conn.execute("SELECT * FROM standalone_chat_memories")}
        sources, replies, memories, active = set(), set(), {}, set()
        accepted = [p for p in original.get("write_proposals", []) if p.get("accepted")]
        proposals = {p["source"]: p for p in accepted}
        check(len(proposals) == len(accepted), "resume_duplicate_accepted_proposal")
        actions = [a for a in original.get("closure_actions", []) if a.get("accepted")]
        expected_writes, expected_closures = set(), {}
        expected_status = {}
        for i, entry in enumerate(entries):
            name, domain, content, kind = steps[i]
            check(entry.get("step") == name and entry.get("turn", {}).get("status") == "succeeded"
                  and (entry.get("passed") is True or (legacy and i == 5)), "resume_turn_incomplete_or_wrong_order")
            source_id = entry.get("source")
            check(source_id not in sources, "resume_duplicate_source")
            source_row = conn.execute("SELECT m.*,member.role FROM standalone_chat_messages m JOIN standalone_chat_members member "
                "ON member.member_id=m.sender_id WHERE m.message_id=?", (source_id,)).fetchone()
            check(source_row is not None and source_row["room_id"] == rooms[domain] and source_row["role"] == "human", "resume_source_scope_changed")
            source_message = json.loads(source_row["message_json"])
            check(source_message["content"] == "@月见 " + content and source_message.get("reply_to") is None
                  and source_message.get("external_source") is None and source_message.get("context_anchor_id") is None,
                  "resume_source_content_changed")
            turns = conn.execute("SELECT * FROM standalone_chat_turns WHERE message_id=?", (source_id,)).fetchall()
            check(len(turns) == 1 and turns[0]["status"] == "succeeded", "resume_turn_state_changed")
            reply_rows = conn.execute("SELECT m.*,member.role FROM standalone_chat_messages m JOIN standalone_chat_members member "
                "ON member.member_id=m.sender_id WHERE m.reply_to=?", (source_id,)).fetchall()
            check(len(reply_rows) == 1 and reply_rows[0]["room_id"] == rooms[domain] and reply_rows[0]["role"] == "implementer"
                  and reply_rows[0]["correlation_id"] == source_row["correlation_id"]
                  and json.loads(reply_rows[0]["message_json"])["content"] == entry["turn"]["reply"], "resume_reply_changed")
            sources.add(source_id)
            replies.add(reply_rows[0]["message_id"])
            contexts = entry.get("context", [])
            check(isinstance(contexts, list), "resume_context_missing")
            for item in contexts:
                mid = item.get("memory_id")
                check(mid in active and mid in mrows and mrows[mid]["room_id"] == rooms[domain]
                      and all(item.get(field) == mrows[mid][field] for field in ("content", "kind", "source_message_id")), "resume_historical_context_changed")
            if kind in {"decision", "correction", "open_loop"}:
                expected_writes.add(source_id)
                proposal = proposals.get(source_id)
                check(proposal is not None and proposal["accepted"] in mrows, "resume_accepted_memory_missing")
                memory = ChatMemoryStore._from_row(mrows[proposal["accepted"]])
                check(str(memory.source_message_id) == source_id and memory.kind == kind and memory.origin == "agent_auto"
                      and str(memory.room_id) == rooms[domain] and memory.content == proposal["proposal"]["content"], "resume_memory_content_changed")
                memories[name] = memory
                active.add(str(memory.memory_id))
                expected_status[str(memory.memory_id)] = ("active", "open", None)
                if kind == "correction":
                    old = memories["create_decision"]
                    check(memory.replaces_memory_id == old.memory_id, "resume_lineage_changed")
                    active.discard(str(old.memory_id))
                    expected_status[str(old.memory_id)] = ("retired", "open", None)
            elif kind in {"complete", "cancel"}:
                memory = memories["create_dev_todo" if kind == "complete" else "create_personal_todo"]
                expected_closures[source_id] = (kind, str(memory.memory_id))
                active.discard(str(memory.memory_id))
                expected_status[str(memory.memory_id)] = ("retired", "completed" if kind == "complete" else "cancelled", source_id)
            else:
                hybrid_query_check(name, entry["turn"]["reply"], contexts, memories)
                if name == "closed_personal_query":
                    check(turns[0]["session_id"] is None and entry.get("closed_state_evidence") == {
                        "memory_id": str(memories["create_personal_todo"].memory_id),
                        "state": "cancelled", "source": "sqlite_status_receipt", "agent_started": False,
                    }, "closed_status_evidence_changed")
                if legacy and name in QUERY_TARGETS:
                    entry["semantic_diagnostic"] = {"status": "historical_miss" if i == 5 else "historical_hit",
                        "expected_hit": i != 5, "hits": None, "threshold": MIN_SIMILARITY,
                        "measurement": "original_v1_assertion_no_saved_scores", "new_embedding_requests": 0}
            if legacy and i == 5:
                entry.update(original_v1_passed=False, passed=True,
                    re_adjudication={"policy": REVISION, "basis": "saved_successful_turn_and_hybrid_prompt",
                                     "parent_report_sha256": report_sha, "new_model_calls": 0})
        check(set(mrows) == set(expected_status), "resume_extra_or_missing_memories")
        check(set(proposals) == expected_writes, "resume_extra_accepted_proposal")
        check(len(actions) == len(expected_closures) and {a.get("source") for a in actions} == set(expected_closures) and all(
            (a["action"].get("action"), a["action"].get("memory_id"))
            == expected_closures.get(a.get("source")) for a in actions), "resume_closure_evidence_changed")
        for mid, state in expected_status.items():
            row = mrows[mid]
            check((row["status"], row["open_loop_state"], row["closed_by_message_id"]) == state, "resume_memory_lifecycle_changed")
        check({r[0] for r in conn.execute("SELECT message_id FROM standalone_chat_messages")} == sources | replies,
              "resume_extra_or_missing_messages")
        check({r[0] for r in conn.execute("SELECT room_id FROM standalone_chat_rooms")} == set(rooms.values()), "resume_extra_or_missing_rooms")
        check(conn.execute("SELECT COUNT(*) FROM standalone_chat_turns").fetchone()[0] == len(entries)
              and conn.execute("SELECT COUNT(*) FROM standalone_chat_turns WHERE session_id IS NOT NULL").fetchone()[0] == used,
              "resume_agent_usage_or_turns_changed")
        counts = {r[0]: r[1] for r in conn.execute("SELECT status,COUNT(*) FROM standalone_chat_vector_outbox WHERE target_id=? GROUP BY status",
                                                (target.backend.sync_target_id,))}
        saved_counts = original["worker_final"].get("diagnostics", {}).get("status_counts", {})
        check(all(counts.get(k, 0) == saved_counts.get(k, 0) for k in ("pending", "processing", "waiting", "dead", "delivered", "superseded"))
              and not any(counts.get(k, 0) for k in ("pending", "processing", "waiting", "dead")), "resume_delivery_state_changed")
    return ResumeState(path, report_sha, entries, {k: UUID(v) for k, v in rooms.items()}, memories, used, embedded,
                       snapshot, legacy, copy.deepcopy(original.get("write_proposals", [])),
                       copy.deepcopy(original.get("closure_actions", [])))
