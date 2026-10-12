"""Resume controls on temporary SQLite + fake adapters; no paid/OS evidence."""

import copy
import hashlib
import json
import urllib.request

import pytest

from app.chat.vector_memory import EmbeddingError, OpenAICompatibleEmbedder
from app.workspace.permissions import PermissionPolicy
from evals import memory_persistent_resume as resume
from evals import run_memory_persistent_live as probe
from tests.test_memory_persistent_live import Adapter, Embeddings, journey_fixture


@pytest.fixture(autouse=True)
def no_paid_or_remote(monkeypatch):
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr(OpenAICompatibleEmbedder, "embed", lambda *a, **k: pytest.fail("paid embedding"))
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *a, **k: pytest.fail("actual process"))


async def prefix_fixture(tmp_path, monkeypatch, count=6):
    journey, remote = journey_fixture(tmp_path)
    full = probe.STEPS
    with monkeypatch.context() as selected:
        selected.setattr(probe, "STEPS", full[:count])
        assert await journey.run()
    target = journey.target
    directory = target.directory / "live-runs" / "20261012T000000Z-12345678"
    directory.mkdir(parents=True)
    path = directory / "report.json"
    report = {
        "manifest": str(target.manifest), "manifest_sha256": target.manifest_sha256,
        "database": str(target.database), "complete": False, "smoke": False,
        "worker_cleanup_complete": True, "worker_exit": 0,
        "worker_final": {"reason": "stopped", "diagnostics": {"status_counts": journey.worker.status()}},
        "limits": {"kimi_starts": 12, "embedding_requests": 40, "kimi_steps_per_turn": 3},
        "kimi_starts": journey.adapter.starts, "embedding_requests": journey.embedder.calls, "embedding_errors": 0,
        "evaluator_revision": resume.REVISION, "min_similarity": resume.MIN_SIMILARITY,
        "steps_sha256": resume.digest(full), "code_sha256": probe.code_fingerprints(),
        "database_snapshot": resume.database_snapshot(target.database), "steps": copy.deepcopy(journey.steps),
        "rooms": {k: str(v) for k, v in journey.rooms.items()},
        "write_proposals": copy.deepcopy(journey.proposals), "closure_actions": copy.deepcopy(journey.actions),
    }
    path.write_text(json.dumps(report, ensure_ascii=False))
    return journey, remote, path, report


def validate(journey, path, **kwargs):
    return resume.validate_resume(journey.target, path, probe.STEPS, probe.code_fingerprints(), **kwargs)


@pytest.mark.asyncio
async def test_v2_read_only_validation_and_remaining_steps_not_replayed(tmp_path, monkeypatch):
    journey, remote, path, _ = await prefix_fixture(tmp_path, monkeypatch)
    original, before = path.read_bytes(), resume.database_snapshot(journey.target.database)
    state = validate(journey, path.parent)
    assert state.summary()["next_step"] == 7 and state.summary()["new_model_calls"] == 0
    assert resume.database_snapshot(journey.target.database) == before
    assert path.read_bytes() == original
    starts, calls = state.kimi_used, state.embedding_used
    adapter = Adapter()
    adapter.starts = starts  # Fake adapter control only; real BudgetKimi offset has its own test.
    embedder = probe.BudgetEmbedder(Embeddings(remote.model.fingerprint), remote.model.dimensions, used=calls)
    async def delivered(memory):
        continued.worker.run_once(limit=32)
    continued = probe.Journey(journey.target, embedder, adapter, tmp_path / "resumed-execution", delivered)
    continued.index._backend = journey.index._backend
    continued.restore(state)
    assert await continued.run()
    assert continued.steps[:6] == state.entries
    assert len(continued.steps) == 12 and all(s["passed"] for s in continued.steps)
    assert continued.adapter.starts > starts and continued.adapter.starts <= 12
    assert continued.embedder.calls > calls and continued.embedder.calls <= 40
    after = resume.database_snapshot(journey.target.database)
    assert after["counts"]["standalone_chat_messages"] - before["counts"]["standalone_chat_messages"] == 12
    assert len(continued.proposals) == 4 and len(continued.actions) == 2
    assert len(remote.points) == 4 and sum(m.record is None for m in remote.points.values()) == 3
    assert probe.finish_audit(continued.worker)["consistent_at_observation"]
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_reviewed_legacy_migration_keeps_original_miss_and_unknown_scores(tmp_path, monkeypatch):
    journey, _, path, report = await prefix_fixture(tmp_path, monkeypatch)
    for key in ("evaluator_revision", "database_snapshot", "steps_sha256", "min_similarity"):
        report.pop(key)
    report["code_sha256"].pop(resume.RESUME_MODULE)
    report["code_sha256"][resume.RUNNER] = resume.LEGACY_RUNNER_SHA
    report["failure_category"] = "direct_semantic_target_missing"
    report["steps"][-1]["passed"] = False
    for entry in report["steps"]:
        entry.pop("semantic_diagnostic", None)
    path.write_text(json.dumps(report))
    original = path.read_bytes()
    with pytest.raises(resume.ResumeError, match="unreviewed_legacy"):
        validate(journey, path)
    state = validate(journey, path, legacy_report_sha=hashlib.sha256(original).hexdigest())
    last = state.entries[-1]
    assert last["passed"] and last["original_v1_passed"] is False
    assert last["semantic_diagnostic"]["expected_hit"] is False
    assert last["semantic_diagnostic"]["hits"] is None
    assert last["re_adjudication"]["new_model_calls"] == 0
    assert path.read_bytes() == original and state.legacy
    # Pinning report bytes is necessary, but does not excuse changed canonical facts.
    with journey.database.connect() as conn:
        conn.execute("UPDATE standalone_chat_memories SET content='tampered' WHERE memory_id=?",
                     (str(journey.memories["create_dev_todo"].memory_id),))
    with pytest.raises(resume.ResumeError, match="resume_.*changed"):
        validate(journey, path, legacy_report_sha=hashlib.sha256(original).hexdigest())


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["manifest", "budget", "usage", "embedding_error", "worker", "smoke",
    "complete", "revision", "dataset", "threshold", "code", "extra_code", "turn", "order", "reply",
    "context", "proposal", "duplicate_proposal", "room", "snapshot", "extra_action"])
async def test_changed_report_refused(tmp_path, monkeypatch, mutation):
    journey, _, path, report = await prefix_fixture(tmp_path, monkeypatch)
    if mutation == "manifest":
        report["manifest_sha256"] = "changed"
    elif mutation == "budget":
        report["limits"]["kimi_starts"] = 13
    elif mutation == "usage":
        report["kimi_starts"] -= 1
    elif mutation == "embedding_error":
        report["embedding_errors"] = 1
    elif mutation == "worker":
        report["worker_cleanup_complete"] = False
    elif mutation in {"smoke", "complete"}:
        report[mutation] = True
    elif mutation == "revision":
        report["evaluator_revision"] = "unknown"
    elif mutation == "dataset":
        report["steps_sha256"] = "changed"
    elif mutation == "threshold":
        report["min_similarity"] = .1
    elif mutation == "code":
        report["code_sha256"]["app/chat/memory.py"] = "changed"
    elif mutation == "extra_code":
        report["code_sha256"]["unknown"] = "changed"
    elif mutation == "turn":
        report["steps"][-1]["passed"] = False
    elif mutation == "order":
        report["steps"][0], report["steps"][1] = report["steps"][1], report["steps"][0]
    elif mutation == "reply":
        report["steps"][-1]["turn"]["reply"] = "changed"
    elif mutation == "context":
        report["steps"][-1]["context"] = []
    elif mutation == "proposal":
        report["write_proposals"][-1]["proposal"]["content"] = "changed"
    elif mutation == "duplicate_proposal":
        report["write_proposals"].append(copy.deepcopy(report["write_proposals"][-1]))
    elif mutation == "room":
        report["rooms"]["personal"] = report["rooms"]["dev"]
    elif mutation == "snapshot":
        report["database_snapshot"]["sha256"] = "changed"
    else:
        report["closure_actions"].append({"source": report["steps"][-1]["source"], "accepted": True, "action": {}})
    path.write_text(json.dumps(report))
    with pytest.raises(resume.ResumeError):
        validate(journey, path)


@pytest.mark.asyncio
@pytest.mark.parametrize("table,column,value", [
    ("standalone_chat_memories", "content", "changed"),
    ("standalone_chat_memories", "status", "active"),
    ("standalone_chat_turns", "status", "failed"),
    ("standalone_chat_vector_outbox", "attempts", 99),
])
async def test_changed_database_refused_before_remote_or_paid(tmp_path, monkeypatch, table, column, value):
    journey, _, path, _ = await prefix_fixture(tmp_path, monkeypatch)
    with journey.database.connect() as conn:
        conn.execute(f'UPDATE "{table}" SET "{column}"=?', (value,))
    with pytest.raises(resume.ResumeError, match="resume_database_changed"):
        validate(journey, path)
    monkeypatch.setattr(probe, "load_target", lambda *a, **k: journey.target)
    monkeypatch.setattr(probe, "check_vector_worker_target", lambda *a, **k: pytest.fail("remote preflight"))
    monkeypatch.setattr(probe, "dotenv_values", lambda *a, **k: pytest.fail("credentials opened"))
    with pytest.raises(resume.ResumeError, match="resume_database_changed"):
        await probe.run_live(journey.target.manifest, resume_from=path)


@pytest.mark.asyncio
async def test_closure_prefix_evidence_validated(tmp_path, monkeypatch):
    journey, _, path, report = await prefix_fixture(tmp_path, monkeypatch, count=10)
    assert validate(journey, path).summary()["next_step"] == 11
    report["closure_actions"][-1]["action"]["action"] = "complete"
    path.write_text(json.dumps(report))
    with pytest.raises(resume.ResumeError, match="resume_closure_evidence_changed"):
        validate(journey, path)


@pytest.mark.asyncio
async def test_duplicate_closure_cannot_hide_missing_other_closure(tmp_path, monkeypatch):
    journey, _, path, report = await prefix_fixture(tmp_path, monkeypatch, count=10)
    report["closure_actions"][-1] = copy.deepcopy(report["closure_actions"][0])
    path.write_text(json.dumps(report))
    with pytest.raises(resume.ResumeError, match="resume_closure_evidence_changed"):
        validate(journey, path)


@pytest.mark.asyncio
async def test_vector_miss_is_diagnostic_but_hybrid_prompt_still_required(tmp_path, monkeypatch):
    journey, _, _, _ = await prefix_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(journey.index, "search_scoped", lambda *a, **k: ())
    diagnostic = journey.semantic(journey.rooms["dev"], "synthetic query", journey.memories["create_dev_todo"])
    assert diagnostic["status"] == "miss" and diagnostic["expected_hit"] is False
    assert diagnostic["hits"] == [] and diagnostic["missing_target_score"] is None
    entry = journey.steps[-1]
    resume.hybrid_query_check(entry["step"], entry["turn"]["reply"], entry["context"], journey.memories)
    with pytest.raises(resume.ResumeError, match="actual_prompt_recall_missing"):
        resume.hybrid_query_check(entry["step"], entry["turn"]["reply"], [], journey.memories)
    # A transport failure is not downgraded to a semantic miss.
    def broken(*a, **k):
        raise EmbeddingError("synthetic transport error")
    monkeypatch.setattr(journey.index, "search_scoped", broken)
    with pytest.raises(EmbeddingError):
        journey.semantic(journey.rooms["dev"], "query", journey.memories["create_dev_todo"])


@pytest.mark.asyncio
async def test_all_vector_queries_miss_bm25_hybrid_journey_still_passes(tmp_path, monkeypatch):
    journey, remote = journey_fixture(tmp_path)
    monkeypatch.setattr(remote, "search_current", lambda *a, **k: ())
    assert await journey.run()
    diagnostics = [s["semantic_diagnostic"] for s in journey.steps if "semantic_diagnostic" in s]
    assert len(diagnostics) == 4 and all(d["expected_hit"] is False for d in diagnostics)
    assert all(s["passed"] for s in journey.steps)


@pytest.mark.asyncio
async def test_schema_change_is_also_checkpoint_drift(tmp_path, monkeypatch):
    journey, _, path, _ = await prefix_fixture(tmp_path, monkeypatch)
    with journey.database.connect() as conn:
        conn.execute("CREATE INDEX standalone_chat_resume_test ON standalone_chat_memories(content)")
    with pytest.raises(resume.ResumeError, match="resume_database_changed"):
        validate(journey, path)


@pytest.mark.parametrize("failure", ["audit", "probe", "backlog", "error", "worker", "receipt", "none"])
def test_completion_requires_successful_final_audit_not_just_journey(failure):
    report = {"journey_passed": True, "current_embedding_probe_passed": True,
        "final_audit": {"scan_complete": True, "consistent_at_observation": True}, "outbox_status": {},
        "worker_cleanup_complete": True, "worker_exit": 0,
        "worker_final": {"event": "watch_exited", "reason": "stopped", "clean_shutdown": True, "diagnostics_status": "observed"}}
    if failure == "audit":
        report["final_audit"]["consistent_at_observation"] = False
    elif failure == "probe":
        report["current_embedding_probe_passed"] = False
    elif failure == "backlog":
        report["outbox_status"]["pending"] = 1
    elif failure == "error":
        report["error_type"] = "RuntimeError"
    elif failure == "worker":
        report["worker_exit"] = 1
    elif failure == "receipt":
        report["worker_final"]["clean_shutdown"] = False
    assert probe.acceptance_complete(report) is (failure == "none")


def test_embedding_budget_offset_reserved_before_call_and_not_reset():
    inner = Embeddings()
    capped = probe.BudgetEmbedder(inner, 2, limit=40, used=39)
    observed = []
    capped.on_attempt = lambda: observed.append((capped.calls, inner.calls))
    capped.embed(["synthetic"])
    with pytest.raises(EmbeddingError, match="budget"):
        capped.embed(["synthetic"])
    assert observed == [(40, 0)] and inner.calls == 1 and capped.used == 39


@pytest.mark.asyncio
async def test_kimi_budget_offset_reserved_before_start_and_not_reset(tmp_path, monkeypatch):
    starts = []
    async def fake_start(self, request):
        starts.append(self.starts)
        return "synthetic"
    monkeypatch.setattr(probe.KimiCodeAdapter, "start", fake_start)
    capped = probe.BudgetKimi(worktree_root=tmp_path / "workspaces", runtime_root=tmp_path / "runtime",
                             policy=PermissionPolicy(allowed_paths=(".",)), used=11)
    reservations = []
    capped.on_attempt = lambda: reservations.append((capped.starts, len(starts)))
    assert await capped.start(None) == "synthetic"
    with pytest.raises(ValueError, match="kimi_turn_budget_exhausted"):
        await capped.start(None)
    assert reservations == [(12, 0)] and starts == [12] and capped.used == 11


@pytest.mark.parametrize("used", [True, -1, 40, 41])
def test_invalid_used_embedding_budget_refused(used):
    with pytest.raises(ValueError, match="used_budget"):
        probe.BudgetEmbedder(Embeddings(), 2, used=used)


def test_advisory_lock_refuses_competitor_then_releases(tmp_path):
    with resume.target_lock(tmp_path), pytest.raises(resume.ResumeError, match="already_locked"), resume.target_lock(tmp_path):
        pytest.fail("competitor acquired lock")
    with resume.target_lock(tmp_path):
        pass


def test_lock_symlink_is_not_followed(tmp_path):
    untouched = tmp_path / "untouched"
    untouched.write_text("sentinel")
    (tmp_path / ".persistent-live.lock").symlink_to(untouched)
    with pytest.raises(OSError), resume.target_lock(tmp_path):
        pytest.fail("symlink followed")
    assert untouched.read_text() == "sentinel"


@pytest.mark.asyncio
async def test_check_resume_cli_no_prompt_model_or_remote(tmp_path, monkeypatch, capsys):
    journey, _, path, _ = await prefix_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(probe, "load_target", lambda *a, **k: journey.target)
    monkeypatch.setattr(probe.getpass, "getpass", lambda *a: pytest.fail("key prompt"))
    monkeypatch.setattr(probe, "check_vector_worker_target", lambda *a, **k: pytest.fail("remote"))
    probe.main(["--check-resume", "--manifest", str(journey.target.manifest), "--resume-from", str(path)])
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["new_model_calls"] == 0


@pytest.mark.asyncio
async def test_report_outside_target_refused(tmp_path, monkeypatch):
    journey, _, path, _ = await prefix_fixture(tmp_path, monkeypatch)
    outside = tmp_path / "report.json"
    outside.write_bytes(path.read_bytes())
    with pytest.raises(resume.ResumeError, match="outside_target"):
        validate(journey, outside)
