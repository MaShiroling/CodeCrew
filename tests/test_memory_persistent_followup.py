"""Two-case entry controls on temporary SQLite + fake Agent/remote only."""

import asyncio
import hashlib
import json
import urllib.request
from pathlib import Path
from uuid import UUID

import pytest

from app.agents import FakeAgentAdapter, FakeAgentScenario
from app.chat.vector_memory import OpenAICompatibleEmbedder
from evals import run_memory_persistent_followup as followup
from evals.memory_persistent_resume import database_snapshot
from scripts import recover_memory_live_miswrite as recovery
from tests.test_memory_persistent_live import journey_fixture


@pytest.fixture(autouse=True)
def no_real_services(monkeypatch):
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr(OpenAICompatibleEmbedder, "embed", lambda *a, **k: pytest.fail("cloud embedding"))
    monkeypatch.setattr(followup.KimiCodeAdapter, "start", lambda *a, **k: pytest.fail("paid Kimi"))


async def fixture(tmp_path, monkeypatch):
    journey, remote = journey_fixture(tmp_path)
    assert await journey.run()
    source = UUID(journey.steps[-1]["source"])
    room = journey.rooms["personal"]
    bad = journey.store.memory.create(room, source, "没有记录就说明未知")
    with journey.database.transaction() as conn:
        conn.execute("UPDATE standalone_chat_memories SET kind='preference',origin='agent_auto' WHERE memory_id=?", (str(bad.memory_id),))
    monkeypatch.setattr(recovery, "BAD_ID", bad.memory_id)
    report = {"manifest_sha256": journey.target.manifest_sha256, "database": str(journey.target.database),
        "worker_cleanup_complete": True, "worker_exit": 0, "rooms": {k: str(v) for k, v in journey.rooms.items()},
        "steps": journey.steps, "database_snapshot": database_snapshot(journey.target.database),
        "kimi_starts": 12, "embedding_requests": 19}  # Synthetic historical ledger, not real billing.
    parent = journey.target.directory / "live-runs" / recovery.REVIEWED_RUN / "report.json"
    parent.parent.mkdir(parents=True)
    parent.write_text(json.dumps(report))
    monkeypatch.setattr(recovery, "REVIEWED_SHA", hashlib.sha256(parent.read_bytes()).hexdigest())
    directory, repaired = recovery.repair(journey.target)
    assert repaired["complete"]
    path = directory / "report.json"
    state = followup.inspect_recovery(journey.target, path)
    return journey, remote, path, state


def adapter(reply="本房间没有记录，API 网关读超时未知。", proposal=None, action=None):
    return FakeAgentAdapter(FakeAgentScenario(output={"message": json.dumps({"content": reply, "handoff_to": [],
        "memory_proposal": proposal, "memory_action": action}, ensure_ascii=False)}))


def test_plan_no_settings_paths_or_keys(monkeypatch, capsys):
    monkeypatch.setattr(followup, "load_target", lambda *a: pytest.fail("opened target"))
    monkeypatch.setattr(followup, "dotenv_values", lambda *a: pytest.fail("keys read"))
    followup.main([])
    assert "Plan only" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_recovered_target_two_queries_one_agent_zero_embedding(tmp_path, monkeypatch):
    journey, _, path, state = await fixture(tmp_path, monkeypatch)
    recovery_raw, parent_raw = path.read_bytes(), state["parent_path"].read_bytes()
    inner = adapter()
    directory, report = await followup.run_cases(journey.target, state, inner, tmp_path / "followup-execution")
    assert report["complete"] and report["followup_passed"] and report["full_lifecycle_passed"] is False
    assert [s["step"] for s in report["steps"]] == [s[0] for s in followup.CASES]
    assert report["steps"][0]["receipt_source"] == "sqlite_cancelled_todo"
    assert report["kimi_starts"] == 1 and len(inner.requests) == 1
    assert report["embedding_requests"] == report["embedding_blocked_attempts"] == 0
    assert report["cumulative_kimi_starts"] == 13 and report["cumulative_embedding_requests"] == 19
    assert not report["background_worker_started"] and not report["paid_retry"]
    assert report["audit"]["consistent_at_observation"] and report["dispatcher_shutdown_complete"]
    assert report["after"]["counts"]["standalone_chat_messages"] - state["before"]["counts"]["standalone_chat_messages"] == 4
    assert followup.memory_snapshot(journey.target.database) == state["memory_before"]
    assert path.read_bytes() == recovery_raw and state["parent_path"].read_bytes() == parent_raw
    assert (directory / "report.json").stat().st_mode & 0o777 == 0o600
    context_line = next(line for line in inner.requests[0].prompt.splitlines() if line.startswith("上下文摘录（JSON）："))
    context = json.loads(context_line.split("：", 1)[1])
    assert context["room_memories"] == context["history"] == []
    with pytest.raises(ValueError, match="already_used"):
        followup.inspect_recovery(journey.target, path)


@pytest.mark.asyncio
async def test_original_miswrite_proposal_is_rejected_not_manually_suppressed(tmp_path, monkeypatch):
    journey, _, _, state = await fixture(tmp_path, monkeypatch)
    _, report = await followup.run_cases(journey.target, state,
        adapter(proposal={"kind": "preference", "content": "没有记录就说明未知"}), tmp_path / "execution")
    assert report["complete"] and report["proposals"][0]["accepted"] is None
    assert report["proposals"][0]["proposal"]["content"] == "没有记录就说明未知"
    assert report["embedding_blocked_attempts"] == 0


@pytest.mark.asyncio
async def test_closed_query_cannot_spend_the_one_kimi_start(tmp_path, monkeypatch):
    journey, _, _, state = await fixture(tmp_path, monkeypatch)
    # Simulate a regression of the deterministic status path.
    monkeypatch.setattr("app.chat.dispatch._closed_open_loop_status_receipt", lambda *a: None)
    inner = adapter()
    _, report = await followup.run_cases(journey.target, state, inner, tmp_path / "execution")
    assert not report["complete"] and len(report["steps"]) == 1
    assert report["kimi_starts"] == 0 and inner.requests == []
    assert report["failure_category"] == "followup_agent_turn_incomplete"


@pytest.mark.parametrize("reply", ["网关已经确定。", "没有记录，默认 20 秒。", "未知，可以设为十秒。", "本房间不是未知，已经拍板了。"])
@pytest.mark.asyncio
async def test_missing_unknown_or_invented_timeout_is_not_passed(tmp_path, monkeypatch, reply):
    journey, _, _, state = await fixture(tmp_path, monkeypatch)
    _, report = await followup.run_cases(journey.target, state, adapter(reply), tmp_path / "execution")
    assert not report["complete"] and report["steps"][0]["passed"]
    assert report["failure_category"] == "followup_unknown_answer_missing_or_invented_value"


@pytest.mark.asyncio
async def test_unexpected_memory_write_retained_and_stops_without_provider_or_repair(tmp_path, monkeypatch):
    journey, _, _, state = await fixture(tmp_path, monkeypatch)
    _, report = await followup.run_cases(journey.target, state,
        adapter(proposal={"kind": "decision", "content": "没有记录就说明未知"}), tmp_path / "execution")
    assert not report["complete"] and report["failure_category"] == "followup_unexpected_memory_mutation"
    assert report["embedding_requests"] == 0 and report["embedding_blocked_attempts"] == 1
    assert report["proposals"][0]["accepted"] is not None  # Actual production guard outcome, no seed or rollback.


@pytest.mark.parametrize("change", ["snapshot", "recovery", "parent", "outside", "audit", "missing_manifest"])
@pytest.mark.asyncio
async def test_preflight_refuses_drift_before_agent_or_any_write(tmp_path, monkeypatch, change):
    journey, _, path, _ = await fixture(tmp_path, monkeypatch)
    if change == "snapshot":
        with journey.database.transaction() as conn:
            conn.execute("UPDATE standalone_chat_memories SET content='changed' WHERE memory_id=?", (str(recovery.BAD_ID),))
    elif change == "parent":
        parent = journey.target.directory / "live-runs" / recovery.REVIEWED_RUN / "report.json"
        parent.write_text("{}")
    elif change == "outside":
        other = tmp_path / "report.json"
        other.write_bytes(path.read_bytes())
        path = other
    else:
        values = json.loads(path.read_text())
        if change == "recovery":
            values["complete"] = False
        elif change == "audit":
            values["audit"]["consistent_at_observation"] = False
        else:
            values["manifest_sha256"] = "changed"
        path.write_text(json.dumps(values))
    before = database_snapshot(journey.target.database)
    with pytest.raises(ValueError):
        followup.inspect_recovery(journey.target, path)
    assert database_snapshot(journey.target.database) == before


@pytest.mark.asyncio
async def test_request_reservation_precedes_only_one_inner_start():
    events = []
    class Inner:
        async def start(self, request):
            events.append("paid")
            return "session"
    wrapped = followup.SingleCallAdapter(Inner())
    with pytest.raises(ValueError, match="closed_case_agent_forbidden"):
        await wrapped.start(None)
    wrapped.enabled = True
    wrapped.on_attempt = lambda: events.append("reserved")
    assert await wrapped.start(None) == "session"
    with pytest.raises(ValueError, match="budget_exhausted"):
        await wrapped.start(None)
    assert events == ["reserved", "paid"] and wrapped.starts == 1


def test_embedding_is_blocked_even_if_indexing_attempts_it():
    embedder = followup.ForbiddenEmbedding("synthetic")
    with pytest.raises(Exception, match="embedding_api_forbidden"):
        embedder.embed(["synthetic"])
    assert embedder.attempts == 1


@pytest.mark.asyncio
async def test_local_check_no_key_prompt_or_remote(tmp_path, monkeypatch, capsys):
    journey, _, path, _ = await fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(followup, "load_target", lambda *a: journey.target)
    monkeypatch.setattr(followup, "check_vector_worker_target", lambda *a: pytest.fail("remote"))
    monkeypatch.setattr(followup.getpass, "getpass", lambda *a: pytest.fail("prompt"))
    before = database_snapshot(journey.target.database)
    followup.main(["--check", "--manifest", str(journey.target.manifest), "--recovery", str(path)])
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["new_model_calls"] == 0
    assert database_snapshot(journey.target.database) == before


@pytest.mark.asyncio
async def test_cancelled_turn_stays_incomplete_and_records_cleanup(tmp_path, monkeypatch):
    journey, _, _, state = await fixture(tmp_path, monkeypatch)
    async def cancel(*a):
        raise asyncio.CancelledError
    monkeypatch.setattr(followup.ObservedDispatcher, "wait_idle", cancel)
    with pytest.raises(asyncio.CancelledError):
        await followup.run_cases(journey.target, state, adapter(), tmp_path / "execution")
    path = next((journey.target.directory / "followup-runs").glob("*/report.json"))
    report = json.loads(path.read_text())
    assert not report["complete"] and report["failure_category"] == "followup_interrupted"
    assert report["kimi_starts"] == 0


def test_no_start_delivery_seed_or_old_budget_reset_in_entry():
    import ast
    tree = ast.parse(Path(followup.__file__).read_text())
    attrs = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not attrs & {"Popen", "initialize", "run_once", "claim", "register_target", "retire", "retry_dead", "create_room", "create"}


@pytest.mark.asyncio
async def test_cleanup_mutation_cannot_be_marked_complete(tmp_path, monkeypatch):
    journey, _, _, state = await fixture(tmp_path, monkeypatch)
    shutdown = followup.ObservedDispatcher.shutdown
    async def changed(self):
        await shutdown(self)
        with self.store.database.transaction() as conn:
            conn.execute("UPDATE standalone_chat_messages SET message_json=json_set(message_json,'$.content','changed') WHERE sequence=1")
    monkeypatch.setattr(followup.ObservedDispatcher, "shutdown", changed)
    _, report = await followup.run_cases(journey.target, state, adapter(), tmp_path / "execution")
    assert report["checks_passed"] and not report["complete"]
    assert report["failure_category"] == "followup_state_changed_during_cleanup"


@pytest.mark.asyncio
async def test_cleanup_error_retains_failed_report_no_raw_exception(tmp_path, monkeypatch):
    journey, _, _, state = await fixture(tmp_path, monkeypatch)
    shutdown = followup.ObservedDispatcher.shutdown
    async def broken(self):
        await shutdown(self)
        raise RuntimeError("synthetic-secret-provider-body")
    monkeypatch.setattr(followup.ObservedDispatcher, "shutdown", broken)
    _, report = await followup.run_cases(journey.target, state, adapter(), tmp_path / "execution")
    assert not report["complete"] and report["dispatcher_shutdown_error_type"] == "RuntimeError"
    assert "synthetic-secret-provider-body" not in json.dumps(report)


@pytest.mark.asyncio
async def test_live_refusal_precedes_credentials_and_remote(tmp_path, monkeypatch):
    journey, _, path, _ = await fixture(tmp_path, monkeypatch)
    path.write_text("{}")
    monkeypatch.setattr(followup, "load_target", lambda *a: journey.target)
    monkeypatch.setattr(followup, "check_vector_worker_target", lambda *a: pytest.fail("remote"))
    monkeypatch.setattr(followup, "dotenv_values", lambda *a: pytest.fail("credentials"))
    with pytest.raises(ValueError):
        await followup.run_live(journey.target.manifest, path)


@pytest.mark.parametrize("args", [["--check", "--live"], ["--check", "--prompt-for-kimi-key"], ["--live"], ["--check"]])
def test_incompatible_or_missing_args_refused(args):
    with pytest.raises(SystemExit) as exc:
        followup.main(args)
    assert exc.value.code == 2
