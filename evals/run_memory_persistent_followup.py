"""Opt-in two-case follow-up on the recovered target, not a 12-step rerun.

One Kimi start (<=3 internal steps), zero embedding API calls. No watch,
delivery, seeding, repair, budget reset, or rewriting old evidence. Failed
turns stop; current snapshot prevents repeated live runs on the same baseline.
"""

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from dotenv import dotenv_values

from app.agents import KimiCodeAdapter
from app.chat.agents import StandaloneChatAgentRuntime, StandaloneChatWorkspaceManager
from app.chat.memory import ChatMemoryStore
from app.chat.qdrant_memory_index import QdrantMemoryVectorIndex
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.chat.vector_maintenance import MemoryVectorMaintenance
from app.chat.vector_memory import EmbeddingError
from app.chat.vector_outbox import MemoryVectorOutbox
from app.chat.vector_worker_contracts import check_vector_worker_target
from app.team.models import MemberRole
from app.workspace.permissions import PermissionPolicy
from evals.memory_persistent_resume import (
    database_snapshot,
    digest,
    hybrid_query_check,
    target_lock,
)
from evals.run_memory_baseline import _turn_result
from evals.run_memory_persistent_live import PROVENANCE, STEPS, ObservedDispatcher
from scripts import recover_memory_live_miswrite as recovery
from scripts.backup_memory_live_database import private_json, readonly
from scripts.run_memory_live_worker import ROOT, load_target, require

CASES = STEPS[10:12]
REVISION = "persistent-followup-v1"
_UNKNOWN = re.compile(r"未知|未确定|尚未确定|还未确定|没有.{0,10}记录|无记录|无法确定|不清楚|不知道|unknown", re.IGNORECASE)
_TIMEOUT_VALUE = re.compile(r"(?:\d+(?:\.\d+)?|[零〇一二三四五六七八九十百两]+)\s*(?:毫秒|秒|ms\b|s\b)", re.IGNORECASE)
_UNKNOWN_DISAVOWED = re.compile(r"(?:不是|并非|不再|并不|不能说)\s*(?:未知|未确定|尚未确定|无记录)")
_FAILURES = frozenset({
    "followup_database_changed_before_write", "followup_agent_turn_incomplete", "followup_unexpected_memory_mutation",
    "followup_embedding_path_attempted", "followup_closed_case_agent_started", "followup_real_agent_missing",
    "followup_unknown_answer_missing_or_invented_value", "followup_backlog_remaining", "followup_audit_incomplete",
    "followup_final_memory_changed", "followup_code_or_evidence_changed", "closed_state_not_confirmed",
    "closed_state_receipt_missing", "closed_or_other_room_context_leak", "other_room_decision_in_answer",
})


def fingerprints():
    return {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
            for p in (*PROVENANCE, "evals/run_memory_persistent_followup.py", "scripts/recover_memory_live_miswrite.py",
                "evals/run_memory_baseline.py", "app/chat/agents.py", "app/chat/store.py", "app/chat/service.py",
                "app/chat/fts_memory.py", "app/chat/fts_text.py", "app/workspace/permissions.py")}


def memory_snapshot(path):
    """Include retired facts, candidates, FTS, cache and delivery, not just active recall."""
    with closing(readonly(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        tables = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if r[0] == "schema_migrations" or r[0].startswith(("standalone_chat_memory", "standalone_chat_memories", "standalone_chat_embedding"))
            or (r[0].startswith("standalone_chat_vector_") and not r[0].startswith("standalone_chat_vector_maintenance")))
        return digest({t: sorted((dict(r) for r in conn.execute('SELECT * FROM "' + t.replace('"', '""') + '"')), key=digest) for t in tables})


def history_snapshot(path, high_water):
    """Protect old messages/turns/receipts and room identity, not only file reports."""
    with closing(readonly(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        body = {t: sorted((dict(r) for r in conn.execute('SELECT * FROM "' + t + '"')), key=digest)
                for t in ("standalone_chat_rooms", "standalone_chat_members", "standalone_chat_discussion_runs")}
        body["messages"] = [dict(r) for r in conn.execute("SELECT * FROM standalone_chat_messages WHERE sequence<=? ORDER BY sequence", (high_water,))]
        for table in ("standalone_chat_turns", "standalone_chat_deliveries"):
            body[table] = sorted((dict(r) for r in conn.execute('SELECT * FROM "' + table + '" WHERE message_id IN '
                '(SELECT message_id FROM standalone_chat_messages WHERE sequence<=?)', (high_water,))), key=digest)
        return digest(body)


def inspect_recovery(target, source):
    path = Path(source).resolve(strict=True)
    if path.is_dir():
        path = (path / "report.json").resolve(strict=True)
    require(path.name == "report.json" and path.parent.parent == target.directory / "recovery-runs", "followup_recovery_outside_target")
    with path.open("rb") as stream:
        raw = stream.read(2_000_001)
    require(len(raw) <= 2_000_000, "followup_recovery_unbounded")
    record = json.loads(raw)
    require(record.get("complete") is True and record.get("phase") == "recovered_and_audited"
        and record.get("operation") == "reviewed_single_memory_retirement"
        and record.get("memory_id") == str(recovery.BAD_ID) and record.get("manifest") == str(target.manifest)
        and record.get("manifest_sha256") == target.manifest_sha256 and record.get("target_id") == target.backend.sync_target_id
        and record.get("audit", {}).get("consistent_at_observation") is True, "followup_recovery_not_verified")
    parent = target.directory / "live-runs" / recovery.REVIEWED_RUN / "report.json"
    parent_raw = parent.read_bytes()
    require(record.get("parent_report") == str(parent) and record.get("parent_report_sha256") == recovery.REVIEWED_SHA
        and hashlib.sha256(parent_raw).hexdigest() == recovery.REVIEWED_SHA, "followup_parent_changed")
    original = json.loads(parent_raw)
    snapshot = database_snapshot(target.database)
    require(snapshot == record.get("after"), "followup_database_changed_or_already_used")
    room = UUID(original["rooms"]["personal"])
    with closing(readonly(target.database)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        bad = conn.execute("SELECT * FROM standalone_chat_memories WHERE memory_id=?", (str(recovery.BAD_ID),)).fetchone()
        require(bad is not None and bad["room_id"] == str(room) and bad["status"] == "retired"
            and bad["retired_reason"] == "manual", "followup_bad_memory_not_retired")
        todo = conn.execute("SELECT * FROM standalone_chat_memories WHERE source_message_id=?",
                            (original["steps"][7]["source"],)).fetchone()
        require(todo is not None and todo["room_id"] == str(room) and todo["status"] == "retired"
            and todo["open_loop_state"] == "cancelled" and todo["closed_by_message_id"] == original["steps"][9]["source"],
            "followup_cancelled_todo_changed")
        require(conn.execute("SELECT COUNT(*) FROM standalone_chat_memories WHERE room_id=? AND status='active'", (str(room),)).fetchone()[0] == 0,
                "followup_personal_room_not_empty")
        require(conn.execute("SELECT COUNT(*) FROM standalone_chat_vector_outbox WHERE status IN ('pending','processing','waiting','dead')").fetchone()[0] == 0,
                "followup_backlog_remaining")
        memory = ChatMemoryStore._from_row(todo)
        high_water = conn.execute("SELECT MAX(sequence) FROM standalone_chat_messages").fetchone()[0]
    return {"recovery_path": path, "recovery_sha256": hashlib.sha256(raw).hexdigest(), "parent_path": parent,
        "room": room, "todo": memory, "before": snapshot, "memory_before": memory_snapshot(target.database),
        "historical_kimi_starts": original["kimi_starts"], "historical_embedding_requests": original["embedding_requests"],
        "history_high_water": high_water, "history_before": history_snapshot(target.database, high_water)}


class ForbiddenEmbedding:
    def __init__(self, fingerprint):
        self.model_fingerprint, self.attempts = fingerprint, 0
        self.on_attempt = lambda: None
        self.lock = threading.Lock()

    def embed(self, texts):
        with self.lock:
            self.attempts += 1
        self.on_attempt()
        raise EmbeddingError("followup_embedding_api_forbidden")  # Never construct/call a provider.


class SingleCallAdapter:
    def __init__(self, inner):
        self.inner, self.enabled, self.starts = inner, False, 0
        self.on_attempt = lambda: None

    async def start(self, request):
        require(self.enabled, "followup_closed_case_agent_forbidden")
        require(self.starts < 1, "followup_kimi_budget_exhausted")
        self.starts += 1
        self.on_attempt()  # Persist reservation before starting the paid process.
        return await self.inner.start(request)

    def stream(self, session_id):
        return self.inner.stream(session_id)

    async def wait(self, session_id):
        return await self.inner.wait(session_id)

    async def cancel(self, session_id):
        return await self.inner.cancel(session_id)


async def run_cases(target, state, inner, execution):
    output = target.directory / "followup-runs" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    adapter = SingleCallAdapter(inner)
    embedder = ForbiddenEmbedding(target.backend.model.fingerprint)
    database = recovery.GuardedDatabase(target.database, state["before"])
    index = QdrantMemoryVectorIndex(database, embedder, target.backend.endpoint)
    store = StandaloneChatStore(database, vector_index=index, lexical_backend="bm25")
    service = StandaloneChatService(store)
    runtime = StandaloneChatAgentRuntime(StandaloneChatWorkspaceManager(execution / "workspaces", execution / "runtime"),
        {r: adapter for r in (MemberRole.PLANNER, MemberRole.IMPLEMENTER, MemberRole.REVIEWER)})
    dispatcher = ObservedDispatcher(store, runtime, max_turns_per_thread=1, timeout_seconds=180)
    report = {"complete": False, "followup_passed": False, "full_lifecycle_passed": False, "revision": REVISION,
        "manifest": str(target.manifest), "manifest_sha256": target.manifest_sha256,
        "recovery_report": str(state["recovery_path"]), "recovery_sha256": state["recovery_sha256"],
        "parent_report": str(state["parent_path"]), "parent_sha256": recovery.REVIEWED_SHA,
        "cases_sha256": digest(CASES),
        "historical_kimi_starts": state["historical_kimi_starts"], "historical_embedding_requests": state["historical_embedding_requests"],
        "limits": {"kimi_starts": 1, "kimi_steps_per_turn": 3, "embedding_requests": 0},
        "semantic_review": "pending_human_review", "before": state["before"], "steps": [], "proposals": [], "actions": [],
        "background_worker_started": False, "paid_retry": False, "code_sha256": fingerprints()}
    def checkpoint():
        report.update(kimi_starts=adapter.starts, embedding_requests=0, embedding_blocked_attempts=embedder.attempts,
            cumulative_kimi_starts=state["historical_kimi_starts"]+adapter.starts,
            cumulative_embedding_requests=state["historical_embedding_requests"])
        temp = output / (uuid4().hex + ".tmp.json")
        private_json(temp, report)
        temp.replace(output / "report.json")
    adapter.on_attempt = embedder.on_attempt = checkpoint
    capture, action = store.memory.capture_agent_proposal, store.memory.capture_agent_action
    def capture_observed(room, source, value):
        memory = capture(room, source, value)
        report["proposals"].append({"source": str(source), "proposal": value.model_dump(mode="json"), "accepted": str(memory.memory_id) if memory else None})
        return memory
    def action_observed(room, source, value):
        memory = action(room, source, value)
        report["actions"].append({"source": str(source), "action": value.model_dump(mode="json"), "accepted": memory is not None})
        return memory
    store.memory.capture_agent_proposal, store.memory.capture_agent_action = capture_observed, action_observed
    checkpoint()
    try:
        require(database_snapshot(target.database) == state["before"], "followup_database_changed_before_write")
        await dispatcher.startup()
        for number, (name, _, content, _) in enumerate(CASES):
            source = service.post_message(state["room"], content="@月见 " + content, idempotency_key=uuid4(), reply_to=None)
            entry = {"step": name, "source": str(source.message.message_id), "passed": False}
            report["steps"].append(entry)
            adapter.enabled = number == 1
            checkpoint()
            print("[followup] " + name, flush=True)
            dispatcher.enqueue(source)
            await asyncio.wait_for(dispatcher.wait_idle(), 210)
            entry["turn"] = _turn_result(store, source)
            require(entry["turn"]["status"] == "succeeded", "followup_agent_turn_incomplete")
            entry["context"] = dispatcher.contexts.get(str(source.message.message_id), [])
            hybrid_query_check(name, entry["turn"]["reply"], entry["context"], {"create_personal_todo": state["todo"]})
            require(memory_snapshot(target.database) == state["memory_before"], "followup_unexpected_memory_mutation")
            require(embedder.attempts == 0, "followup_embedding_path_attempted")
            turns = [t for t in store.list_turns(state["room"]) if t.message_id == source.message.message_id]
            if number == 0:
                require(adapter.starts == 0 and len(turns) == 1 and turns[0].session_id is None, "followup_closed_case_agent_started")
                entry["receipt_source"] = "sqlite_cancelled_todo"
            else:
                require(adapter.starts == 1 and len(turns) == 1 and turns[0].session_id is not None, "followup_real_agent_missing")
                require(_UNKNOWN.search(entry["turn"]["reply"]) and not _TIMEOUT_VALUE.search(entry["turn"]["reply"])
                    and not _UNKNOWN_DISAVOWED.search(entry["turn"]["reply"]), "followup_unknown_answer_missing_or_invented_value")
            entry["passed"] = True
            checkpoint()
        worker = MemoryVectorOutbox(database, target.backend)
        report["outbox_status"] = worker.status()
        require(not any(report["outbox_status"].get(k, 0) for k in ("pending", "processing", "waiting", "dead")), "followup_backlog_remaining")
        maintenance = MemoryVectorMaintenance(worker)
        audit_id = maintenance.start(mode="audit")
        report["audit_run_id"] = str(audit_id)
        checkpoint()
        for _ in range(8):
            report["audit"] = maintenance.step(audit_id, limit=16)
            checkpoint()
            if report["audit"]["scan_complete"]:
                break
        require(report["audit"]["consistent_at_observation"], "followup_audit_incomplete")
        require(memory_snapshot(target.database) == state["memory_before"], "followup_final_memory_changed")
        require(fingerprints() == report["code_sha256"] and hashlib.sha256(state["recovery_path"].read_bytes()).hexdigest() == state["recovery_sha256"]
            and hashlib.sha256(state["parent_path"].read_bytes()).hexdigest() == recovery.REVIEWED_SHA
            and hashlib.sha256(target.manifest.read_bytes()).hexdigest() == target.manifest_sha256, "followup_code_or_evidence_changed")
        report["checks_passed"] = True
    except asyncio.CancelledError:
        report.update(error_type="CancelledError", failure_category="followup_interrupted")
        raise
    except Exception as exc:  # noqa: BLE001 - no raw SQL/provider/credential output
        report["error_type"] = type(exc).__name__
        report["failure_category"] = str(exc) if str(exc) in _FAILURES else "followup_checkpoint_failed"
    finally:
        try:
            await dispatcher.shutdown()
            report["dispatcher_shutdown_complete"] = True
        except Exception as exc:  # noqa: BLE001
            report["dispatcher_shutdown_error_type"] = type(exc).__name__
        report["after"] = database_snapshot(target.database)
        report["final_state_unchanged"] = bool(memory_snapshot(target.database) == state["memory_before"]
            and history_snapshot(target.database, state["history_high_water"]) == state["history_before"]
            and report["after"]["counts"]["standalone_chat_messages"] == state["before"]["counts"]["standalone_chat_messages"] + 4
            and report["after"]["counts"]["standalone_chat_turns"] == state["before"]["counts"]["standalone_chat_turns"] + 2
            and fingerprints() == report["code_sha256"]
            and hashlib.sha256(state["recovery_path"].read_bytes()).hexdigest() == state["recovery_sha256"]
            and hashlib.sha256(state["parent_path"].read_bytes()).hexdigest() == recovery.REVIEWED_SHA
            and hashlib.sha256(target.manifest.read_bytes()).hexdigest() == target.manifest_sha256)
        report["complete"] = bool(report.get("checks_passed") and report.get("dispatcher_shutdown_complete") and not report.get("error_type")
            and report["final_state_unchanged"] and adapter.starts == 1 and embedder.attempts == 0)
        if report.get("checks_passed") and not report["final_state_unchanged"]:
            report["failure_category"] = "followup_state_changed_during_cleanup"
        report["followup_passed"] = report["complete"]
        checkpoint()
    return output, report


async def run_live(manifest, recovery_path, *, key=None, prompt_key=False):
    target = load_target(manifest)
    with target_lock(target.directory):
        state = inspect_recovery(target, recovery_path)
        check_vector_worker_target(target.database, target.backend)  # Remote metadata only.
        key = getpass.getpass("Kimi Code key: ") if prompt_key else key
        key = key or os.environ.get("KIMI_MODEL_API_KEY") or dotenv_values(ROOT / ".env").get("KIMI_MODEL_API_KEY")
        executable = shutil.which(target.settings.kimi_cli_path)
        require(isinstance(key, str) and key.strip() and executable is not None, "followup_kimi_credentials_and_cli_required")
        execution = Path.home() / ".codecrew/memory-live-runtime" / target.directory.name / ("followup-" + uuid4().hex)
        inner = KimiCodeAdapter(worktree_root=execution / "workspaces", runtime_root=execution / "runtime",
            policy=PermissionPolicy(allowed_paths=(".",)), executable=executable,
            env_source={"KIMI_MODEL_API_KEY": key.strip(), "PATH": os.environ.get("PATH", os.defpath)},
            allow_standalone_chat=True, max_steps_per_turn=3)
        return await run_cases(target, state, inner, execution)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="local read-only preflight, no models/remote/processes")
    modes.add_argument("--live", action="store_true", help="post exactly two new queries, at most one paid Kimi start")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--recovery", type=Path, help="verified recovery report/directory on the same target")
    parser.add_argument("--prompt-for-kimi-key", action="store_true")
    args = parser.parse_args(argv)
    if args.prompt_for_kimi_key and not args.live:
        parser.error("key prompt requires --live")
    if not args.live and not args.check:
        print("Plan only: cases 11/12; new cap 1 Kimi start, <=3 internal steps, embedding API forbidden; old results/budgets unchanged.")
        return
    if args.manifest is None or args.recovery is None:
        parser.error("explicit --manifest and --recovery required")
    try:
        if args.check:
            state = inspect_recovery(load_target(args.manifest), args.recovery)
            print(json.dumps({"validated": True, "cases": [s[0] for s in CASES], "new_kimi_cap": 1,
                "embedding_api_cap": 0, "new_model_calls": 0, "recovery_sha256": state["recovery_sha256"]}))
            return
        output, report = asyncio.run(run_live(args.manifest, args.recovery, prompt_key=args.prompt_for_kimi_key))
        print("Results: " + str(output / "report.json"))
        if not report["complete"]:
            parser.exit(1, "Follow-up incomplete; no automatic paid retry; evidence retained\n")
        print("Two-case follow-up passed; original failed report unchanged; human answer review still required")
    except Exception as exc:  # noqa: BLE001 - fixed type only
        parser.exit(1, "Follow-up refused: " + type(exc).__name__ + "\n")


if __name__ == "__main__":
    main()
