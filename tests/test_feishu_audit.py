"""Second audit: deterministic crash windows, ordering and worker ownership."""

import asyncio
import time

import pytest
from feishu_helpers import inbound, requests, rows, setup

from app.chat.discussion_runs import DiscussionRunStatus


@pytest.mark.asyncio
async def test_busy_notice_cannot_overtake_unscanned_agent_reply(tmp_path):
    h = setup(tmp_path)
    first = await h.bridge.receive(inbound())
    await h.dispatcher.wait_idle()
    # A new run keeps the room busy after the previous replies were persisted,
    # while the outbox poller has not scanned them yet.
    await h.bridge.receive(inbound(2))
    assert (await h.bridge.receive(inbound(3))).disposition == "busy"
    try:
        assert await h.outbox.deliver_one()
        assert "第一条" in h.sender.sent[0][1]
        assert h.sender.sent[0][2] == "om_1"
    finally:
        await h.dispatcher.wait_idle()
    assert h.dispatcher.runs.get(first.run_id).status is DiscussionRunStatus.FINISHED


@pytest.mark.asyncio
async def test_busy_notice_at_same_sequence_follows_agent_and_terminal(tmp_path):
    h = setup(tmp_path)
    first = await h.bridge.receive(inbound())
    # Hold completion after the last Agent message is already durable.
    original = h.dispatcher.runs.complete_turn

    def complete(*args, **kwargs):
        if kwargs["reply"].next_action.value == "finish":
            h.bridge._admit(inbound(2), h.service.external_opening_role("讨论"))
        return original(*args, **kwargs)

    h.dispatcher.runs.complete_turn = complete
    await h.dispatcher.wait_idle()
    h.outbox.scan()
    while await h.outbox.deliver_one():
        pass
    assert h.dispatcher.runs.get(first.run_id).status is DiscussionRunStatus.FINISHED
    assert "第三条" in h.sender.sent[2][1]
    assert "已结束" in h.sender.sent[3][1]
    assert "进行中" in h.sender.sent[4][1]


def blocked_worker(*args):
    """Real spawned process, no SDK or network; must be killed on cancellation."""
    while True:
        time.sleep(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_sender_timeout_or_cancellation_reaps_real_worker_and_can_restart(monkeypatch, cancel):
    import multiprocessing

    from pydantic import SecretStr

    from app.feishu import sender as module

    monkeypatch.setattr(module, "require_sdk", lambda: None)
    monkeypatch.setattr(module, "_sdk_send_worker", blocked_worker)
    sender = module.OfficialFeishuSender("cli_fake", SecretStr("fake"))
    sender.timeout_seconds = 0.2 if not cancel else 30
    before = {child.pid for child in multiprocessing.active_children()}
    for _ in range(2):
        task = asyncio.create_task(sender.send_text("oc_dm", "text"))
        while sender._process is None:
            await asyncio.sleep(0.01)
        process = sender._process
        assert process.is_alive()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else module.FeishuSendError):
            await task
        assert sender._process is None and process._closed
        assert {child.pid for child in multiprocessing.active_children()} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["finish_ingress", "schedule"])
async def test_interrupted_admission_does_not_leave_created_run_busy_forever(tmp_path, monkeypatch, boundary):
    h = setup(tmp_path)
    target, method = (h.store, "finish_ingress") if boundary == "finish_ingress" else (h.dispatcher, "start")
    original = getattr(target, method)

    def fail(*args, **kwargs):
        raise RuntimeError("injected admission failure")

    monkeypatch.setattr(target, method, fail)
    with pytest.raises(RuntimeError, match="injected"):
        await h.bridge.receive(inbound())
    monkeypatch.setattr(target, method, original)
    replay = await h.bridge.receive(inbound())
    run = h.dispatcher.runs.get(replay.run_id)
    assert run.status is DiscussionRunStatus.INTERRUPTED
    assert requests(h) == []
    h.outbox.scan()
    assert len(rows(h, "feishu_outbox")) == 1
    assert (await h.bridge.receive(inbound(2))).disposition == "admitted"
    await h.dispatcher.wait_idle()


@pytest.mark.asyncio
async def test_remote_success_before_receipt_commit_can_duplicate_but_never_reruns_agent(tmp_path, monkeypatch):
    h = setup(tmp_path)
    await h.bridge.receive(inbound())
    await h.dispatcher.wait_idle()

    def crash(*args):
        raise RuntimeError("receipt commit lost")

    monkeypatch.setattr(h.store, "sent", crash)
    with pytest.raises(RuntimeError, match="receipt commit lost"):
        await h.outbox.deliver_one()
    assert len(h.sender.sent) == 1
    assert rows(h, "feishu_outbox")[0]["status"] == "sending"
    restarted = setup(tmp_path)
    await restarted.dispatcher.startup()
    restarted.bridge.recover()
    restarted.store.recover_sending(force=True)
    assert await restarted.outbox.deliver_one()
    assert restarted.sender.sent[0] == h.sender.sent[0]  # Honest at-least-once boundary.
    assert rows(restarted, "feishu_outbox")[0]["attempt_count"] == 2
    assert requests(restarted) == [] and len(requests(h)) == 3


@pytest.mark.asyncio
async def test_reply_persisted_before_turn_completion_survives_restart(tmp_path, monkeypatch):
    h = setup(tmp_path)

    def crash(*args, **kwargs):
        raise RuntimeError("turn completion lost")

    monkeypatch.setattr(h.dispatcher.runs, "complete_turn", crash)
    first = await h.bridge.receive(inbound())
    await h.dispatcher.wait_idle()
    assert len(h.chat.list_messages(first.room_id)) == 2
    assert rows(h, "feishu_outbox") == []
    restarted = setup(tmp_path)
    await restarted.dispatcher.startup()
    restarted.bridge.recover()
    while await restarted.outbox.deliver_one():
        pass
    assert len(restarted.sender.sent) == 2
    assert "第一条" in restarted.sender.sent[0][1]
    assert "中断" in restarted.sender.sent[1][1]
    assert requests(restarted) == []


def test_concurrent_first_binding_and_run_creation_use_database_guards(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from uuid import uuid4

    from app.chat.store import StandaloneChatConflictError
    from app.feishu.privacy import safe_identifier
    from app.feishu.store import platform_key
    from app.team.models import MemberRole

    h, other = setup(tmp_path), setup(tmp_path)
    barrier = Barrier(2)

    def bind(instance):
        assert instance.store.binding("oc_dm") is None
        barrier.wait(timeout=5)
        room = instance.service.create_room(title=f"飞书会话 {safe_identifier('oc_dm')}",
            idempotency_key=platform_key("cli_test", "chat", "oc_dm"))
        return instance.store.bind("oc_dm", room.room_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = list(pool.map(bind, (h, other)))
    assert left["room_id"] == right["room_id"] and len(rows(h, "feishu_bindings")) == 1
    from uuid import UUID
    room_id = UUID(left["room_id"])
    roots = [h.service.post_message(room_id, content="@白金 concurrent", idempotency_key=uuid4(), reply_to=None)
             for _ in range(2)]

    def create(pair):
        instance, root = pair
        barrier.wait(timeout=5)
        try:
            return instance.dispatcher.runs.create(root, opening_role=MemberRole.PLANNER)
        except StandaloneChatConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ((h, roots[0]), (other, roots[1]))))
    assert sum(result is not None for result in results) == 1
    assert len(h.dispatcher.runs.list_for_room(room_id)) == 1
    assert requests(h) == requests(other) == []


@pytest.mark.asyncio
async def test_cancelled_receiver_stop_finishes_cleanup(monkeypatch):
    from types import SimpleNamespace

    from pydantic import SecretStr
    from test_feishu_transport import Process, Queue

    from app.feishu import transport as module

    monkeypatch.setattr(module, "require_sdk", lambda: None)
    monkeypatch.setattr(module.multiprocessing, "get_context", lambda mode: SimpleNamespace(Queue=Queue, Process=Process))
    receiver = module.OfficialFeishuTransport("cli_fake", SecretStr("fake"))
    await receiver.start()
    process = receiver._process
    stop = asyncio.create_task(receiver.stop())
    await asyncio.sleep(0)
    stop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop
    assert process.closed and not process.alive
    assert receiver._pump_task is receiver._process is None
    assert receiver._events.closed and receiver._states.closed


def test_sender_worker_error_with_closed_parent_pipe_does_not_escape(monkeypatch):
    from app.feishu import sender as module

    class ClosedPipe:
        closed = False

        def send(self, value):
            raise BrokenPipeError("parent stopped")

        def close(self):
            self.closed = True

    def broken_sdk():
        raise RuntimeError("credential-must-not-appear-in-child-traceback")

    monkeypatch.setattr(module, "require_sdk", broken_sdk)
    result = ClosedPipe()
    module._sdk_send_worker("cli_fake", "fake", "oc_fake", "text", None, result)
    assert result.closed
