"""Opt-in single Kimi + real embedding + actual worker on an existing target.

Plan opens nothing. No Codex/DeepSeek/HTTP server/browser/old-data imports.
12 synthetic Human steps, <=12 Agent starts and <=40 embedding attempts; stop
on failure without paid retries. Raw synthetic replies stay in private evidence.
"""

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values

from app.agents import KimiCodeAdapter
from app.chat.agents import StandaloneChatAgentRuntime, StandaloneChatWorkspaceManager
from app.chat.qdrant_memory_index import QdrantMemoryVectorIndex
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.chat.vector_memory import EmbeddingEndpoint, EmbeddingError, OpenAICompatibleEmbedder
from app.chat.vector_outbox import MemoryVectorOutbox
from app.chat.vector_worker_contracts import check_vector_worker_target
from app.storage import SQLiteDatabase
from app.team.models import MemberRole
from app.workspace.permissions import PermissionPolicy
from evals.benchmark_memory_qdrant import finish_audit
from evals.memory_persistent_resume import (
    MIN_SIMILARITY,
    QUERY_TARGETS,
    REVISION,
    database_snapshot,
    digest,
    hybrid_query_check,
    target_lock,
    validate_resume,
)
from evals.run_memory_baseline import _SingleAgentEvalDispatcher, _turn_result
from scripts.backup_memory_live_database import inventory, readonly
from scripts.run_memory_live_worker import (
    PROVENANCE as WORKER_PROVENANCE,
)
from scripts.run_memory_live_worker import (
    ROOT,
    child_environment,
    cleanup_child,
    load_target,
    require,
    wait_until,
)

STEPS = (
    ("create_decision", "dev", "API 网关的读超时统一定为 8 秒。", "decision"),
    ("recall_decision", "dev", "网关等待后端响应最多多久？", "query"),
    ("replace_decision", "dev", "纠正上一条决定：API 网关的读超时改为 12 秒。", "correction"),
    ("recall_replacement", "dev", "网关现在的读超时是多少秒？", "query"),
    ("create_dev_todo", "dev", "待办：发布前完成 gateway loadtest 压测。", "open_loop"),
    ("recall_dev_todo", "dev", "这次发布前还有什么未完成的工作？", "query"),
    ("complete_dev_todo", "dev", "gateway loadtest 压测已经完成。", "complete"),
    ("create_personal_todo", "personal", "待办：预约 dentist checkup 牙科检查。", "open_loop"),
    ("recall_personal_todo", "personal", "下次牙科复查还要安排什么？", "query"),
    ("cancel_personal_todo", "personal", "取消 dentist checkup 牙科检查。", "cancel"),
    ("closed_personal_query", "personal", "dentist checkup 牙科检查还在未完成待办里吗？", "query"),
    ("other_room_isolation", "personal", "本房间是否已确定 API 网关读超时？没有记录就说明未知。", "query"),
)
PROVENANCE = (*WORKER_PROVENANCE, "evals/run_memory_persistent_live.py", "app/chat/dispatch.py",
              "app/chat/memory.py", "app/chat/qdrant_memory_index.py", "app/agents/kimi.py",
              "evals/memory_persistent_resume.py")


class BudgetEmbedder:
    def __init__(self, inner, dimensions, *, limit=40, used=0):
        require(type(limit) is int and 1 <= limit <= 40, "embedding_limit_required")
        require(type(used) is int and 0 <= used < limit, "embedding_used_budget_invalid")
        self.inner, self.dimensions, self.limit = inner, dimensions, limit
        self.model_fingerprint = inner.model_fingerprint
        self.calls, self.used, self.errors = used, used, 0
        self.on_attempt = lambda: None
        self.lock = threading.Lock()

    def embed(self, texts):
        with self.lock:
            if self.calls >= self.limit:
                self.errors += 1
                raise EmbeddingError("acceptance_embedding_budget_exhausted")
            self.calls += 1  # Attempts, including HTTP failures, consume budget.
        self.on_attempt()  # Persist budget reservation before a paid request.
        try:
            vectors = self.inner.embed(texts)
            if any(len(v) != self.dimensions for v in vectors):
                raise EmbeddingError("acceptance_dimension_changed")
            return vectors
        except Exception:
            with self.lock:
                self.errors += 1
            raise


class BudgetKimi(KimiCodeAdapter):
    def __init__(self, *args, limit=12, used=0, **kwargs):
        require(type(limit) is int and 1 <= limit <= 12, "kimi_turn_limit_required")
        require(type(used) is int and 0 <= used < limit, "kimi_used_budget_invalid")
        super().__init__(*args, **kwargs)
        self.limit, self.starts, self.used = limit, used, used
        self.on_attempt = lambda: None

    async def start(self, request):
        require(self.starts < self.limit, "kimi_turn_budget_exhausted")
        self.starts += 1
        self.on_attempt()
        return await super().start(request)


class ObservedDispatcher(_SingleAgentEvalDispatcher):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.contexts = {}

    def _prompt(self, role, stored, room):
        prompt = super()._prompt(role, stored, room)
        line = next(line for line in prompt.splitlines() if line.startswith("上下文摘录（JSON）："))
        self.contexts[str(stored.message.message_id)] = json.loads(line.split("：", 1)[1])["room_memories"]
        return prompt  # Do not add gold answers, seed memory or change production guards.


class Journey:
    def __init__(self, target, embedder, adapter, execution_root, delivered):
        self.target, self.embedder, self.adapter, self.delivered = target, embedder, adapter, delivered
        self.database = SQLiteDatabase(target.database)
        self.index = QdrantMemoryVectorIndex(self.database, embedder, target.backend.endpoint, min_similarity=MIN_SIMILARITY)
        self.store = StandaloneChatStore(self.database, vector_index=self.index, lexical_backend="bm25")
        self.service = StandaloneChatService(self.store)
        workspaces = StandaloneChatWorkspaceManager(execution_root / "workspaces", execution_root / "runtime")
        runtime = StandaloneChatAgentRuntime(workspaces, {r: adapter for r in (
            MemberRole.PLANNER, MemberRole.IMPLEMENTER, MemberRole.REVIEWER)})
        self.dispatcher = ObservedDispatcher(self.store, runtime, max_turns_per_thread=1, timeout_seconds=180)
        self.worker = MemoryVectorOutbox(self.database, target.backend)
        self.rooms, self.memories, self.steps = {}, {}, []
        self.proposals, self.actions = [], []
        capture, close = self.store.memory.capture_agent_proposal, self.store.memory.capture_agent_action
        def proposal(room, source, value):
            memory = capture(room, source, value)
            self.proposals.append({"source": str(source), "proposal": value.model_dump(mode="json"),
                                   "accepted": str(memory.memory_id) if memory else None})
            return memory
        def action(room, source, value):
            memory = close(room, source, value)
            self.actions.append({"source": str(source), "action": value.model_dump(mode="json"), "accepted": memory is not None})
            return memory
        self.store.memory.capture_agent_proposal, self.store.memory.capture_agent_action = proposal, action

    def remote(self, memory, tombstone=False):
        with self.database.connect() as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT * FROM standalone_chat_vector_revisions WHERE memory_id=?", (str(memory.memory_id),)).fetchone()
            mutation, waiting = self.worker.desired_in_snapshot(conn, row)
        require(waiting is None and (mutation.record is None) == tombstone, "canonical_cache_or_tombstone_mismatch")
        observed = self.target.backend.inspect_mutations((mutation,))[0]
        require(observed.code == "current" and observed.remote_revision == mutation.revision, "remote_sync_revision_mismatch")

    def semantic(self, room, query, memory):
        # Direct vector branch, not lexical fallback passing as vector success.
        before = self.embedder.calls
        hits = self.index.search_scoped(query, self.store.memory.list_active(room), room_id=room)
        hit = memory.memory_id in {mid for mid, _ in hits}
        return {"status": "hit" if hit else "miss", "expected_hit": hit,
                "hits": [{"memory_id": str(mid), "similarity": score} for mid, score in hits],
                "threshold": MIN_SIMILARITY, "measurement": "fresh_direct_vector_query",
                "new_embedding_requests": self.embedder.calls-before,
                "missing_target_score": None}  # Threshold-filtered misses do not expose their score.

    def restore(self, state):
        self.rooms, self.memories = dict(state.rooms), dict(state.memories)
        self.steps = json.loads(json.dumps(state.entries))
        self.proposals, self.actions = list(state.proposals), list(state.actions)

    async def run(self, *, smoke=False, checkpoint=lambda: None):
        await self.dispatcher.startup()
        try:
            selected = STEPS[:2] if smoke else STEPS
            for name, domain, content, kind in selected[len(self.steps):]:
                if domain not in self.rooms:
                    self.rooms[domain] = self.service.create_room(title="Real acceptance " + domain, idempotency_key=uuid4()).room_id
                room = self.rooms[domain]
                source = self.service.post_message(room, content="@月见 " + content, idempotency_key=uuid4(), reply_to=None)
                entry = {"step": name, "source": str(source.message.message_id), "passed": False, "turn": None}
                self.steps.append(entry)
                checkpoint()
                print("[live] " + name, flush=True)
                self.dispatcher.enqueue(source)
                await asyncio.wait_for(self.dispatcher.wait_idle(), 210)
                entry["turn"] = _turn_result(self.store, source)
                require(entry["turn"]["status"] == "succeeded", "agent_turn_incomplete_not_memory_miss")
                require(self.embedder.errors == 0, "embedding_error_or_budget_not_memory_miss")
                contexts = self.dispatcher.contexts.get(str(source.message.message_id), [])
                entry["context"] = contexts
                created = [m for m in self.store.memory.list_active(room) if m.source_message_id == source.message.message_id]
                if kind in {"decision", "correction", "open_loop"}:
                    require(len(created) == 1 and created[0].origin == "agent_auto" and created[0].kind == kind,
                            "expected_auto_write_missing_or_wrong_kind")
                    memory = created[0]
                    self.memories[name] = memory
                    if kind == "correction":
                        old = self.memories["create_decision"]
                        require(memory.replaces_memory_id == old.memory_id, "explicit_replacement_lineage_missing")
                        await self.delivered(old)
                        self.remote(old, tombstone=True)
                    await self.delivered(memory)
                    self.remote(memory)
                elif kind in {"complete", "cancel"}:
                    memory = self.memories["create_dev_todo" if kind == "complete" else "create_personal_todo"]
                    closed = [m for m in self.store.memory.list_closed_open_loops(room) if m.memory_id == memory.memory_id]
                    require(len(closed) == 1 and closed[0].closed_by_message_id == source.message.message_id
                            and closed[0].open_loop_state == ("completed" if kind == "complete" else "cancelled"),
                            "expected_grounded_closure_missing")
                    await self.delivered(closed[0])
                    self.remote(closed[0], tombstone=True)
                    self.memories["create_dev_todo" if kind == "complete" else "create_personal_todo"] = closed[0]
                    require(memory.memory_id not in {m.memory_id for m in self.store.memory.list_active(room)}, "closed_memory_still_active")
                else:
                    require(not created, "question_incorrectly_auto_written")
                    hybrid_query_check(name, entry["turn"]["reply"], contexts, self.memories)
                    if name == "closed_personal_query":
                        turns = [t for t in self.store.list_turns(room) if t.message_id == source.message.message_id]
                        require(len(turns) == 1 and turns[0].session_id is None, "closed_status_unexpected_agent_start")
                        entry["closed_state_evidence"] = {
                            "memory_id": str(self.memories["create_personal_todo"].memory_id),
                            "state": "cancelled", "source": "sqlite_status_receipt", "agent_started": False,
                        }
                    if name in QUERY_TARGETS:
                        entry["semantic_diagnostic"] = self.semantic(room, content, self.memories[QUERY_TARGETS[name]])
                require(self.embedder.errors == 0, "semantic_embedding_error_not_memory_miss")
                # Current source is excluded even if relevant text is identical.
                require(not self.index.search_scoped(content, tuple(created), room_id=room,
                    exclude_correlation_id=source.message.correlation_id), "same_discussion_semantic_leak")
                entry["passed"] = True
                checkpoint()
            return True
        finally:
            await self.dispatcher.shutdown()


def code_fingerprints():
    return {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in PROVENANCE}


def acceptance_complete(report):
    """Do not turn successful turns + clean shutdown into a passed failed audit."""
    final = report.get("worker_final", {})
    audit = report.get("final_audit", {})
    return bool(report.get("journey_passed") and report.get("current_embedding_probe_passed")
        and audit.get("scan_complete") and audit.get("consistent_at_observation")
        and not any(report.get("outbox_status", {}).get(s, 0) for s in ("pending", "processing", "waiting", "dead"))
        and report.get("worker_cleanup_complete") and report.get("worker_exit") == 0
        and final.get("event") == "watch_exited" and final.get("reason") == "stopped"
        and final.get("clean_shutdown") and final.get("diagnostics_status") == "observed"
        and not any(key.endswith("error_type") or key == "failure_category" for key in report))


async def run_live(manifest, *, smoke=False, kimi_key=None, resume_from=None):
    target = load_target(manifest)
    with target_lock(target.directory):
        return await _run_locked(target, smoke=smoke, kimi_key=kimi_key, resume_from=resume_from)


async def _run_locked(target, *, smoke=False, kimi_key=None, resume_from=None):
    require(not (smoke and resume_from), "resume_full_run_only")
    fingerprints = code_fingerprints()
    resumed = validate_resume(target, resume_from, STEPS, fingerprints) if resume_from else None
    if resumed is None:
        require(inventory(target.database)["counts"]["standalone_chat_messages"] == 0, "fresh_existing_acceptance_target_required")
    check_vector_worker_target(target.database, target.backend)  # Fresh local+remote preflight before paid work.
    endpoint = EmbeddingEndpoint(target.settings.memory_embedding_base_url, target.settings.memory_embedding_model,
        target.settings.memory_embedding_revision, target.settings.memory_embedding_api_key.get_secret_value(),
        target.settings.memory_embedding_timeout_seconds)
    require(endpoint.model_fingerprint == target.backend.model.fingerprint, "live_embedding_identity_changed")
    key = kimi_key or os.environ.get("KIMI_MODEL_API_KEY") or dotenv_values(ROOT / ".env").get("KIMI_MODEL_API_KEY")
    executable = shutil.which(target.settings.kimi_cli_path)
    require(isinstance(key, str) and key.strip() and executable is not None, "local_kimi_credentials_and_cli_required")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    output = target.directory / "live-runs" / run_id
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    # Preserve SQLite in its existing location; execution must be outside Git.
    execution = Path.home() / ".codecrew/memory-live-runtime" / target.directory.name / run_id
    workspaces = StandaloneChatWorkspaceManager(execution / "workspaces", execution / "runtime")
    embedder = BudgetEmbedder(OpenAICompatibleEmbedder(endpoint), target.backend.model.dimensions,
        limit=10 if smoke else 40, used=resumed.embedding_used if resumed else 0)
    adapter = BudgetKimi(worktree_root=workspaces.workspace_root, runtime_root=workspaces.runtime_root,
        policy=PermissionPolicy(allowed_paths=(".",)), executable=executable,
        env_source={"KIMI_MODEL_API_KEY": key.strip(), "PATH": os.environ.get("PATH", os.defpath)},
        allow_standalone_chat=True, max_steps_per_turn=3, limit=2 if smoke else 12, used=resumed.kimi_used if resumed else 0)
    report = {"complete": False, "smoke": smoke, "manifest": str(target.manifest), "manifest_sha256": target.manifest_sha256,
              "database": str(target.database), "execution_root": str(execution), "steps": [],
              "limits": {"kimi_starts": adapter.limit, "embedding_requests": embedder.limit, "kimi_steps_per_turn": 3},
              "agent": "single_kimi_implementer_production_dispatch_not_browser_or_http",
              "semantic_review": "pending_human_review", "code_sha256": fingerprints,
              "evaluator_revision": REVISION, "steps_sha256": digest(STEPS), "min_similarity": MIN_SIMILARITY,
              "resume_from": str(resumed.report_path) if resumed else None,
              "parent_report_sha256": resumed.report_sha256 if resumed else None,
              "inherited_kimi_starts": adapter.used, "inherited_embedding_requests": embedder.used,
              "legacy_re_adjudication": resumed.legacy if resumed else False}
    if resumed:
        report.update(steps=resumed.entries, rooms={k: str(v) for k, v in resumed.rooms.items()},
                      write_proposals=resumed.proposals, closure_actions=resumed.actions)
    proc, journey, streams = None, None, []
    checkpoint_lock = threading.RLock()
    def checkpoint():
        with checkpoint_lock:
            report.update(kimi_starts=adapter.starts, embedding_requests=embedder.calls, embedding_errors=embedder.errors,
                          new_kimi_starts=adapter.starts-adapter.used, new_embedding_requests=embedder.calls-embedder.used,
                          database_snapshot=database_snapshot(target.database))
            if journey is not None:
                report.update(steps=journey.steps, rooms={k: str(v) for k, v in journey.rooms.items()},
                              write_proposals=journey.proposals, closure_actions=journey.actions)
            tmp = output / "report.tmp.json"
            with tmp.open("w", encoding="utf-8") as stream:
                os.chmod(tmp, 0o600)
                json.dump(report, stream, ensure_ascii=False, indent=2)
            tmp.replace(output / "report.json")
    checkpoint()
    embedder.on_attempt = adapter.on_attempt = checkpoint
    try:
        if resumed:
            require(database_snapshot(target.database) == resumed.snapshot, "database_changed_before_resume")
        embedder.embed(["CodeCrew 真实记忆验收维度探测。"])
        report["current_embedding_probe_passed"] = True
        for label in ("worker-stdout.jsonl", "worker-stderr.log"):
            path = output / label
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            streams.append(os.fdopen(fd, "w"))
        launch = asyncio.create_task(asyncio.to_thread(subprocess.Popen,
            [sys.executable, "-m", "scripts.run_memory_live_worker", "--watch", "--manifest", str(target.manifest)],
            cwd=output, env=child_environment(), stdin=subprocess.DEVNULL,
            stdout=streams[0], stderr=streams[1], start_new_session=True))
        try:
            proc = await asyncio.shield(launch)
        except asyncio.CancelledError:
            # Retain ownership if cancellation overlaps Popen creation; finally
            # can still stop/reap the actual child rather than orphaning it.
            proc = await launch
            raise
        def ready():
            require(proc.poll() is None, "live_worker_exited")
            lines = (output / "worker-stdout.jsonl").read_text().splitlines()
            if not lines:
                return False
            try:
                value = json.loads(lines[0])
            except json.JSONDecodeError:
                return False
            require(value == {"event": "preflight_passed", "pid": proc.pid, "target_id": target.backend.sync_target_id}, "worker_identity_mismatch")
            return True
        await asyncio.to_thread(wait_until, ready)
        async def delivered(memory):
            def done():
                require(proc.poll() is None, "worker_exited_before_sync")
                with target_readonly(target.database) as conn:
                    row = conn.execute("SELECT status FROM standalone_chat_vector_outbox WHERE target_id=? AND memory_id=? "
                        "ORDER BY revision DESC LIMIT 1", (target.backend.sync_target_id, str(memory.memory_id))).fetchone()
                return row is not None and row[0] == "delivered"
            await asyncio.to_thread(wait_until, done, timeout=20)
        journey = Journey(target, embedder, adapter, execution, delivered)
        if resumed:
            journey.restore(resumed)
            print("Reusing " + str(len(resumed.entries)) + " completed Human turns; next step " + str(len(resumed.entries)+1), flush=True)
        report["journey_passed"] = await journey.run(smoke=smoke, checkpoint=checkpoint)
        report["final_audit"] = finish_audit(journey.worker)
        report["outbox_status"] = journey.worker.status()
        require(not any(report["outbox_status"].get(s, 0) for s in ("pending", "processing", "waiting", "dead")), "live_backlog_remaining")
        require(all(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == sha for p, sha in report["code_sha256"].items()), "live_code_changed")
    except Exception as exc:  # noqa: BLE001 - raw private replies already stay in private evidence only
        report["error_type"] = type(exc).__name__
        report["failure_category"] = str(exc) if str(exc) in {
            "agent_turn_incomplete_not_memory_miss", "embedding_error_or_budget_not_memory_miss",
            "expected_auto_write_missing_or_wrong_kind", "explicit_replacement_lineage_missing",
            "expected_grounded_closure_missing", "actual_prompt_recall_missing", "direct_semantic_target_missing",
            "question_incorrectly_auto_written", "closed_state_not_confirmed", "closed_state_receipt_missing",
            "closed_status_unexpected_agent_start", "closed_or_other_room_context_leak",
            "semantic_embedding_error_not_memory_miss", "other_room_decision_in_answer",
        } else "acceptance_checkpoint_failed"
    finally:
        if proc is not None:
            try:
                # Graceful stop first; cleanup fallback only our tracked child.
                if proc.poll() is None:
                    proc.terminate()
                    await asyncio.to_thread(proc.wait, timeout=15)
                report["worker_exit"] = proc.returncode
            except Exception as exc:  # noqa: BLE001
                report["worker_cleanup_error_type"] = type(exc).__name__
            finally:
                try:
                    report["worker_cleanup_complete"] = cleanup_child(proc)
                    report["worker_exit"] = proc.returncode
                except Exception as exc:  # noqa: BLE001
                    report["worker_cleanup_complete"] = False
                    report["worker_cleanup_error_type"] = type(exc).__name__
        else:
            report["worker_cleanup_complete"] = True
        for stream in streams:
            stream.close()
        if proc is not None and report.get("worker_cleanup_complete"):
            try:
                lines = (output / "worker-stdout.jsonl").read_text().splitlines()
                final = json.loads(lines[-1])
                require(final["event"] == "watch_exited" and final["reason"] == "stopped"
                        and final["clean_shutdown"] and final["diagnostics_status"] == "observed", "worker_final_receipt_missing")
                report["worker_final"] = final
            except Exception as exc:  # noqa: BLE001
                report["worker_receipt_error_type"] = type(exc).__name__
        report["complete"] = acceptance_complete(report)
        report["full_lifecycle_passed"] = bool(report["complete"] and not smoke)
        report["semantic_branch"] = [s["semantic_diagnostic"] for s in journey.steps if "semantic_diagnostic" in s] if journey else []
        checkpoint()
    return output, report


def target_readonly(path):
    return closing(readonly(path))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--prompt-for-kimi-key", action="store_true")
    parser.add_argument("--resume-from", type=Path, help="validated prior full-run report/directory; never overwrite or replay prefix")
    parser.add_argument("--check-resume", action="store_true", help="read-only local resume validation; no remote/model/process calls")
    args = parser.parse_args(argv)
    if not args.live and not args.check_resume:
        print("Plan only: existing target; 12 synthetic Human steps (smoke 2), single Kimi <=12 starts + embedding <=40 attempts "
              "(smoke 2/10); actual worker, production dispatch, remote revisions/tombstones/vector/prompt/audit. "
              "Hybrid acceptance + separate vector diagnostics; validated --resume-from reuses prefix/cumulative budgets; "
              "Private execution outside Git; no Codex/DeepSeek/browser/HTTP/old data/paid retries; no Settings or keys opened.")
        return
    if args.manifest is None:
        parser.error("explicit --manifest required")
    if args.check_resume:
        if args.live or args.smoke or args.prompt_for_kimi_key or args.resume_from is None:
            parser.error("--check-resume needs --resume-from and cannot run/prompt/smoke")
        try:
            state = validate_resume(load_target(args.manifest), args.resume_from, STEPS, code_fingerprints())
        except Exception as exc:  # noqa: BLE001 - safe category only
            parser.exit(1, f"Resume refused: {type(exc).__name__}\n")
        print(json.dumps(state.summary(), ensure_ascii=False))
        return
    key = getpass.getpass("Kimi Code key: ") if args.prompt_for_kimi_key else None
    try:
        output, report = asyncio.run(run_live(args.manifest, smoke=args.smoke, kimi_key=key, resume_from=args.resume_from))
    except Exception as exc:  # noqa: BLE001 - no Settings/env/provider details
        parser.exit(1, f"Live acceptance refused: {type(exc).__name__}\n")
    print(f"Results: {output / 'report.json'}")
    if not report["complete"]:
        parser.exit(1, "Live acceptance incomplete; no automatic paid retry\n")
    print("Live journey passed; worker stopped; full lifecycle=" + str(report["full_lifecycle_passed"]))


if __name__ == "__main__":
    main()
