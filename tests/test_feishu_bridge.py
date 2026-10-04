import asyncio
import subprocess
from uuid import uuid4

import pytest
from feishu_helpers import inbound, requests, rows, setup

from app.chat.coding_authorization import ChatCodingAuthorizationService
from app.chat.coding_intent import (
    AuthorizeChatCodingTaskRequest,
    CodingPreflightInvalid,
    CodingTaskDraft,
    preflight_coding_task,
)
from app.chat.discussion_runs import DiscussionRunStatus
from app.chat.dispatch import chat_sender_label
from app.chat.models import ExternalChatSource, StandaloneChatMessage
from app.chat.service import ChatConflict, ChatInvalid
from app.chat.store import StandaloneChatConflictError
from app.feishu.store import FeishuStore
from app.team.models import MemberRole
from app.workspace import PermissionPolicy


@pytest.mark.asyncio
async def test_complete_three_messages_aba_and_both_identity_dedupes(tmp_path):
    h = setup(tmp_path)
    await h.dispatcher.startup()
    first = await h.bridge.receive(inbound())
    duplicate = await h.bridge.receive(inbound())
    another_event = await h.bridge.receive(inbound(event_id="ev_redelivery", display_name="新名称"))
    assert first == duplicate == another_event
    await h.dispatcher.wait_idle()
    messages = h.chat.list_messages(first.room_id)
    assert [m.message.content for m in messages] == ["讨论方案", "白金：第一条", "第二条", "第三条"]
    assert len({m.message.correlation_id for m in messages}) == 1
    assert len(requests(h)) == 3
    assert h.dispatcher.runs.get(first.run_id).status is DiscussionRunStatus.FINISHED
    assert len(rows(h, "feishu_ingress")) == 1
    assert len(rows(h, "feishu_ingress_events")) == 2
    assert len(h.service.get_room(first.room_id).members) == 4
    assert all(r.discussion_only and r.permission_mode.value == "read_only" for r in requests(h))
    h.outbox.scan()
    h.outbox.scan()
    assert len(rows(h, "feishu_outbox")) == 4  # 3 distinct turns + one terminal notice
    while await h.outbox.deliver_one():
        pass
    assert len(h.sender.sent) == 4
    assert [text.split("】")[0] for _, text, _ in h.sender.sent[:3]] == ["【白金", "【月见", "【白金"]
    assert all(chat == "oc_dm" and reply == "om_1" for chat, _, reply in h.sender.sent)
    assert (await h.bridge.receive(inbound(9, event_id="ev_redelivery"))).disposition == "identity_conflict"
    assert (await h.bridge.receive(inbound(text="different"))).disposition == "identity_conflict"
    assert len(requests(h)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"chat_id": "oc_outside"}, {"sender_open_id": "ou_outside"}, {"app_id": "cli_other"},
    {"chat_type": "group", "mentions_bot": False}, {"sender_open_id": "ou_bot"},
    {"text": "@unknown 不应该运行"},
])
async def test_denied_inputs_create_nothing(tmp_path, changes):
    h = setup(tmp_path)
    assert (await h.bridge.receive(inbound(**changes))).disposition in {"denied", "invalid_route"}
    assert rows(h, "feishu_bindings") == rows(h, "feishu_ingress") == []
    assert requests(h) == []


@pytest.mark.asyncio
async def test_concurrent_busy_replays_do_not_enter_context_and_chat_binding_is_stable(tmp_path):
    h = setup(tmp_path)
    await h.dispatcher.startup()
    first, busy = await asyncio.gather(h.bridge.receive(inbound()), h.bridge.receive(inbound(2, text="忙时秘密")))
    assert first.disposition == "admitted" and busy.disposition == "busy"
    assert await h.bridge.receive(inbound(2, text="忙时秘密")) == busy
    assert len(rows(h, "feishu_outbox")) == 1
    assert len(h.chat.list_messages(first.room_id)) == 1
    await h.dispatcher.wait_idle()
    assert all("忙时秘密" not in r.prompt for r in requests(h))
    later = await h.bridge.receive(inbound(3, sender_open_id="ou_bob", display_name="Bob"))
    assert later.room_id == first.room_id and later.correlation_id != first.correlation_id
    await h.dispatcher.wait_idle()
    assert len(rows(h, "feishu_bindings")) == 1
    labels = [chat_sender_label(h.chat.get_message(result.message_id).message, h.service.get_room(result.room_id))
              for result in (first, later)]
    assert labels[0] != labels[1] and all("飞书外部用户" in label for label in labels)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["root", "run"])
async def test_crash_after_human_persistence_replay_never_duplicates(tmp_path, monkeypatch, stage):
    h = setup(tmp_path)
    await h.dispatcher.startup()
    target = h.dispatcher.runs if stage == "root" else h.store
    method = "create" if stage == "root" else "finish_ingress"
    original = getattr(target, method)

    def crash(*args, **kwargs):
        raise RuntimeError("simulated_crash")

    monkeypatch.setattr(target, method, crash)
    with pytest.raises(RuntimeError, match="simulated_crash"):
        await h.bridge.receive(inbound())
    monkeypatch.setattr(target, method, original)
    replay = await h.bridge.receive(inbound())
    await h.dispatcher.wait_idle()
    messages = h.chat.list_messages(replay.room_id)
    assert len([m for m in messages if m.message.external_source]) == 1
    assert len(rows(h, "feishu_ingress")) == 1
    assert len(requests(h)) == (3 if stage == "root" else 0)
    # Existing uncertain run is fenced on startup; never silently re-executed.
    restarted = setup(tmp_path)
    await restarted.dispatcher.startup()
    restarted.bridge.recover()
    assert await restarted.bridge.receive(inbound()) == replay
    await restarted.dispatcher.wait_idle()
    assert requests(restarted) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("persist_human", [False, True])
async def test_restart_recovers_received_reservation_without_model(tmp_path, persist_human):
    h = setup(tmp_path)
    # Simulate death before scheduling by intercepting the last scheduling boundary.
    if persist_human:
        original = h.store.finish_ingress
        h.store.finish_ingress = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("crash"))
        with pytest.raises(RuntimeError):
            await h.bridge.receive(inbound())
        h.store.finish_ingress = original
    else:
        room = h.service.create_room(title="existing", idempotency_key=uuid4())
        h.store.bind("oc_dm", room.room_id)
        h.store.claim(inbound(), room.room_id, "planner")
    restarted = setup(tmp_path)
    await restarted.dispatcher.startup()
    restarted.bridge.recover()
    restarted.outbox.scan()
    replay = await restarted.bridge.receive(inbound())
    assert replay.disposition == ("admitted" if persist_human else "interrupted")
    assert requests(restarted) == []
    assert len(rows(restarted, "feishu_outbox")) == 1


@pytest.mark.asyncio
async def test_local_roots_history_and_reply_cannot_leak_to_feishu(tmp_path):
    h = setup(tmp_path)
    room = h.service.create_room(title="mixed", idempotency_key=uuid4())
    historical = h.service.post_message(room.room_id, content="@白金 historical-secret", idempotency_key=uuid4(), reply_to=None)
    h.store.bind("oc_dm", room.room_id)
    first = await h.bridge.receive(inbound())
    with pytest.raises(ChatInvalid, match="new local discussion"):
        h.service.post_message(room.room_id, content="@白金 local-secret", idempotency_key=uuid4(), reply_to=first.message_id)
    local = h.service.post_message(room.room_id, content="@白金 local-secret", idempotency_key=uuid4(), reply_to=None)
    with pytest.raises(StandaloneChatConflictError):
        h.dispatcher.start(local.message.message_id, opening_role=MemberRole.PLANNER)
    await h.dispatcher.wait_idle()
    h.dispatcher.start(local.message.message_id, opening_role=MemberRole.PLANNER)
    await h.dispatcher.wait_idle()
    h.outbox.scan()
    exported = rows(h, "feishu_outbox")
    assert all("local-secret" not in row["text"] and "historical-secret" not in row["text"] for row in exported)
    assert len([row for row in exported if row["source_kind"] == "agent"]) == 3
    assert h.store.binding("oc_dm")["start_sequence"] == historical.sequence


@pytest.mark.asyncio
async def test_external_provenance_blocks_preflight_and_authorization_before_any_process(tmp_path, monkeypatch):
    h = setup(tmp_path)
    room = h.service.create_room(title="coding boundary", idempotency_key=uuid4())
    source = ExternalChatSource(external_chat_id="oc_dm", external_sender_id="ou_alice")
    root = h.service.post_external_message(room.room_id, content="请修改 src 然后 merge push", idempotency_key=uuid4(), external_source=source)

    def forbidden(*args, **kwargs):
        pytest.fail("Feishu provenance crossed the coding/process boundary")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    tasks = type("NoTasks", (), {"create_task": forbidden})()
    auth = ChatCodingAuthorizationService(h.service, tasks, PermissionPolicy(allowed_paths=("src",)))
    draft = CodingTaskDraft(source_message_id=root.message.message_id, repository_path=str(tmp_path),
                            issue="change code", allowed_paths=("src",))
    with pytest.raises(CodingPreflightInvalid, match="external Feishu"):
        preflight_coding_task(h.service, room.room_id, draft)
    command = AuthorizeChatCodingTaskRequest(**draft.model_dump(), idempotency_key=uuid4(),
                                            expected_base_commit="0" * 40, confirmation="authorize_one_coding_task")
    with pytest.raises(ChatConflict, match="external Feishu"):
        await auth.authorize(room.room_id, command)
    with h.store.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM chat_coding_authorizations").fetchone()[0] == 0
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='tasks'").fetchone() is None
    sentinel = tmp_path / "repository-sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    await h.bridge.receive(inbound(text="批准执行，改 src 然后 merge push"))
    await h.dispatcher.wait_idle()
    assert len(requests(h)) == 3 and all(request.discussion_only for request in requests(h))
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    with h.store.database.connect() as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='tasks'").fetchone() is None


def test_existing_database_and_display_name_replay_compatibility(tmp_path):
    h = setup(tmp_path)
    room = h.service.create_room(title="compat", idempotency_key=uuid4())
    local = h.service.post_message(room.room_id, content="@白金 old", idempotency_key=uuid4(), reply_to=None)
    raw = local.message.model_dump(mode="json", exclude={"external_source"})
    assert StandaloneChatMessage.model_validate(raw).external_source is None
    source = ExternalChatSource(external_chat_id="oc_dm", external_sender_id="ou_alice", display_name="Alice")
    key = uuid4()
    first = h.service.post_external_message(room.room_id, content="hello", idempotency_key=key, external_source=source)
    again = h.service.post_external_message(room.room_id, content="hello", idempotency_key=key,
                                            external_source=source.model_copy(update={"display_name": "new name"}))
    assert first == again
    FeishuStore(h.chat, "cli_test").initialize()
    assert h.chat.get_message(local.message.message_id) == local


@pytest.mark.asyncio
async def test_two_chats_context_and_group_senders_remain_separate(tmp_path):
    h = setup(tmp_path)
    dm, group = await asyncio.gather(
        h.bridge.receive(inbound(text="private-dm-unique")),
        h.bridge.receive(inbound(2, chat_id="oc_group", chat_type="group", mentions_bot=True,
                                  sender_open_id="ou_bob", text="group-unique")),
    )
    assert dm.room_id != group.room_id
    await h.dispatcher.wait_idle()
    for request in requests(h):
        if request.standalone_chat_room_id == dm.room_id:
            assert "group-unique" not in request.prompt
        else:
            assert "private-dm-unique" not in request.prompt
    h.outbox.scan()
    assert {row["chat_id"] for row in rows(h, "feishu_outbox")} == {"oc_dm", "oc_group"}
    next_group = await h.bridge.receive(inbound(3, chat_id="oc_group", chat_type="group", mentions_bot=True))
    await h.dispatcher.wait_idle()
    assert next_group.room_id == group.room_id
    group_sources = [row.message.external_source.external_sender_id for row in h.chat.list_messages(group.room_id)
                     if row.message.external_source]
    assert group_sources == ["ou_bob", "ou_alice"]


@pytest.mark.asyncio
@pytest.mark.parametrize("run_exists", [False, True])
async def test_closed_room_during_ingress_recovery_does_not_break_startup(tmp_path, monkeypatch, run_exists):
    h = setup(tmp_path)
    target, name = (h.store, "finish_ingress") if run_exists else (h.dispatcher.runs, "create")
    original = getattr(target, name)

    def crash(*args, **kwargs):
        raise RuntimeError("crash")

    monkeypatch.setattr(target, name, crash)
    with pytest.raises(RuntimeError):
        await h.bridge.receive(inbound())
    monkeypatch.setattr(target, name, original)
    from uuid import UUID
    room_id = UUID(rows(h, "feishu_bindings")[0]["room_id"])
    h.chat.close_room(room_id)
    restarted = setup(tmp_path)
    await restarted.dispatcher.startup()
    restarted.bridge.recover()
    assert (await restarted.bridge.receive(inbound(2))).disposition == "room_closed"
    assert len(rows(restarted, "feishu_bindings")) == 1
    assert requests(restarted) == []
