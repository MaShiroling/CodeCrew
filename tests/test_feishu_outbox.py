import logging
from datetime import datetime, timedelta

import pytest
from feishu_helpers import SequenceAgent, inbound, release_retry, reply, requests, rows, setup

from app.agents import FakeAgentScenario
from app.chat.discussion_runs import DiscussionRun, DiscussionRunStatus
from app.feishu.privacy import safe_outbound
from app.orchestration.models import utc_now
from app.team.models import MemberRole


@pytest.mark.asyncio
async def test_two_send_failures_then_success_preserves_order_without_inference(tmp_path):
    h = setup(tmp_path, failures=2)
    await h.bridge.receive(inbound())
    await h.dispatcher.wait_idle()
    h.outbox.scan()
    for attempt in (1, 2):
        assert await h.outbox.deliver_one()
        first = rows(h, "feishu_outbox")[0]
        assert first["status"] == "retry_wait" and first["attempt_count"] == attempt
        delay = (datetime.fromisoformat(first["next_retry_at"]) - datetime.fromisoformat(first["updated_at"])).total_seconds()
        assert delay == 2 ** attempt
        assert not await h.outbox.deliver_one()  # waiting head blocks the later turns
        assert h.sender.sent == []
        release_retry(h)
    while await h.outbox.deliver_one():
        pass
    assert len(h.sender.sent) == 4
    assert "第一条" in h.sender.sent[0][1] and "第二条" in h.sender.sent[1][1]
    assert len(requests(h)) == 3
    assert h.store.counts() == {"binding_count": 1, "pending_outbox_count": 0, "retry_count": 2, "failed_count": 0}


@pytest.mark.asyncio
async def test_agent_persisted_before_scan_and_stale_sending_restart(tmp_path):
    h = setup(tmp_path)
    await h.bridge.receive(inbound())
    await h.dispatcher.wait_idle()
    assert rows(h, "feishu_outbox") == []
    restarted = setup(tmp_path)
    await restarted.dispatcher.startup()
    restarted.bridge.recover()
    restarted.outbox.scan()
    claim = restarted.store.claim_delivery(max_attempts=5)
    assert claim["status"] == "sending" and claim["attempt_count"] == 1
    assert restarted.store.recover_sending() == 0
    assert restarted.store.recover_sending(force=True) == 1
    assert await restarted.outbox.deliver_one()
    assert rows(restarted, "feishu_outbox")[0]["attempt_count"] == 2
    restarted.outbox.scan()
    while await restarted.outbox.deliver_one():
        pass
    assert requests(restarted) == []
    assert len(restarted.sender.sent) == 4


@pytest.mark.asyncio
async def test_retry_exhaustion_is_finite_and_releases_ordered_queue(tmp_path):
    h = setup(tmp_path, failures=100, feishu_max_outbox_attempts=2)
    await h.bridge.receive(inbound())
    await h.dispatcher.wait_idle()
    h.outbox.scan()
    for _ in range(8):
        assert await h.outbox.deliver_one()
        release_retry(h)
    assert not await h.outbox.deliver_one()
    assert all(row["status"] == "failed" and row["attempt_count"] == 2 for row in rows(h, "feishu_outbox"))
    assert len(requests(h)) == 3


@pytest.mark.asyncio
async def test_scan_insert_and_cursor_rollback_together(tmp_path, monkeypatch):
    h = setup(tmp_path)
    await h.bridge.receive(inbound())
    await h.dispatcher.wait_idle()
    original = h.store.enqueue

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("crash after insert")

    monkeypatch.setattr(h.store, "enqueue", crash)
    with pytest.raises(RuntimeError):
        h.outbox.scan()
    assert rows(h, "feishu_outbox") == []
    assert h.store.binding("oc_dm")["scan_cursor"] == 0
    monkeypatch.setattr(h.store, "enqueue", original)
    h.outbox.scan()
    assert len(rows(h, "feishu_outbox")) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["chat", "sender", "binding"])
async def test_changed_allowlists_or_disabled_binding_stop_pending_sends(tmp_path, change):
    h = setup(tmp_path)
    await h.bridge.receive(inbound())
    await h.dispatcher.wait_idle()
    h.outbox.scan()
    if change == "binding":
        with h.store.database.transaction() as conn:
            conn.execute("UPDATE feishu_bindings SET status='disabled'")
    elif change == "sender":
        h.settings.feishu_allowed_sender_open_ids = frozenset()
    else:
        h.settings.feishu_allowed_chat_ids = frozenset()
    while await h.outbox.deliver_one():
        pass
    assert h.sender.sent == []
    assert h.store.counts()["failed_count"] == 4


@pytest.mark.asyncio
async def test_loop_hits_existing_turn_budget_and_emits_one_notice(tmp_path):
    h = setup(tmp_path, adapters={
        MemberRole.PLANNER: SequenceAgent(reply("A", "handoff", ("implementer",))),
        MemberRole.IMPLEMENTER: SequenceAgent(reply("B", "handoff", ("planner",))),
        MemberRole.REVIEWER: SequenceAgent(reply()),
    })
    ingress = await h.bridge.receive(inbound())
    await h.dispatcher.wait_idle()
    run = h.dispatcher.runs.get(ingress.run_id)
    assert run.status.value == "limit_reached" and run.agent_turns_used == 8
    h.outbox.scan()
    h.outbox.scan()
    assert len(rows(h, "feishu_outbox")) == 9
    assert sum(row["source_kind"] == "status" for row in rows(h, "feishu_outbox")) == 1


@pytest.mark.parametrize("content", [
    "fake-secret-unique", "API_KEY=do-not-export", "stderr: diagnostic", "Traceback (most recent call last):",
    "C:\\Users\\private\\file.txt", "/home/private/token", "hidden_tests/test_private.py",
    "/tmp", "file:///Users/private/file.txt",
    "-----BEGIN PRIVATE KEY-----", "sk-abcdefghijklmnopqrstuvwxyz",
])
def test_sensitive_outbound_withheld(content):
    result = safe_outbound(content, secrets=("fake-secret-unique",))
    assert content not in result


@pytest.mark.asyncio
async def test_logs_and_status_do_not_contain_credentials_ids_or_content(tmp_path, caplog):
    h = setup(tmp_path)
    with caplog.at_level(logging.INFO):
        await h.bridge.receive(inbound(text="unique-private-prompt"))
        await h.dispatcher.wait_idle()
        h.outbox.scan()
        await h.outbox.deliver_one()
    exposed = caplog.text + h.runtime.status().model_dump_json() + repr(h.settings)
    for value in ("fake-secret-unique", "cli_test", "oc_dm", "ou_alice", "om_1", "ev_1", "unique-private-prompt"):
        assert value not in exposed


@pytest.mark.asyncio
async def test_existing_wall_time_limit_is_enforced_at_feishu_ingress(tmp_path, monkeypatch):
    h = setup(tmp_path)
    monkeypatch.setattr(h.dispatcher, "_schedule", lambda run_id: None)
    ingress = await h.bridge.receive(inbound())
    run = h.dispatcher.runs.get(ingress.run_id)
    assert run.limits.max_agent_turns == 8 and run.limits.max_elapsed_seconds == 600
    now = utc_now()
    aged = DiscussionRun.model_validate({**run.model_dump(), "status": DiscussionRunStatus.RUNNING,
        "created_at": now - timedelta(seconds=602), "started_at": now - timedelta(seconds=601), "updated_at": now})
    with h.store.database.transaction() as conn:
        conn.execute("UPDATE standalone_chat_discussion_runs SET run_json=? WHERE run_id=?", (aged.model_dump_json(), str(run.run_id)))
    assert h.dispatcher.runs.claim_next(run.run_id) is None
    assert h.dispatcher.runs.get(run.run_id).stop_reason.value == "time_limit"
    h.outbox.scan()
    assert len(rows(h, "feishu_outbox")) == 1 and requests(h) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["await_human", "failed"])
async def test_waiting_and_failure_notices_are_safe_and_deduplicated(tmp_path, outcome):
    scenario = reply("请补充范围", "await_human") if outcome == "await_human" else FakeAgentScenario(start_error="secret-in-stderr")
    h = setup(tmp_path, adapters={role: SequenceAgent(scenario) for role in (MemberRole.PLANNER, MemberRole.IMPLEMENTER, MemberRole.REVIEWER)})
    await h.bridge.receive(inbound())
    await h.dispatcher.wait_idle()
    h.outbox.scan()
    h.outbox.scan()
    records = rows(h, "feishu_outbox")
    assert sum(row["source_kind"] == "status" for row in records) == 1
    assert all("secret-in-stderr" not in row["text"] for row in records)
