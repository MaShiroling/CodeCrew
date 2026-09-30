"""Fake-agent acceptance for repository-free chat dispatch and safe replay."""

import asyncio
import json
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.agents import AgentExitReason, FakeAgentAdapter, FakeAgentScenario
from app.chat.agents import StandaloneChatAgentRuntime, StandaloneChatWorkspaceManager
from app.chat.dispatch import StandaloneChatDispatcher
from app.chat.models import ChatTurnStatus, StandaloneChatMessage
from app.chat.service import StandaloneChatService
from app.chat.store import StandaloneChatStore
from app.main import create_app
from app.storage import SQLiteDatabase
from app.team.models import MemberRole


def prompt_context(prompt: str) -> dict:
    prefix = "上下文摘录（JSON）："
    line = next(line for line in prompt.splitlines() if line.startswith(prefix))
    return json.loads(line[len(prefix):])


def setup(tmp_path: Path, *, planner=None, implementer=None, reviewer=None, max_turns=6):
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "chat.sqlite3"))
    store.initialize()
    adapters = {
        MemberRole.PLANNER: planner or FakeAgentAdapter(FakeAgentScenario(output={
            "message": '{"content":"白金建议先确定输入类型","handoff_to":["implementer"]}',
        })),
        MemberRole.IMPLEMENTER: implementer or FakeAgentAdapter(FakeAgentScenario(output={
            "message": '{"content":"月见建议覆盖空值和 Unicode","handoff_to":["reviewer"]}',
        })),
        MemberRole.REVIEWER: reviewer or FakeAgentAdapter(FakeAgentScenario(output={
            "message": '{"content":"鲸鲸建议加入回归用例","handoff_to":[]}',
        })),
    }
    runtime = StandaloneChatAgentRuntime(
        StandaloneChatWorkspaceManager(tmp_path / "workspaces", tmp_path / "runtime"),
        adapters,
    )
    dispatcher = StandaloneChatDispatcher(store, runtime, max_turns_per_thread=max_turns)
    return StandaloneChatService(store), dispatcher, adapters


@pytest.mark.asyncio
async def test_http_mention_triggers_bounded_agent_chat_without_task(tmp_path: Path) -> None:
    service, dispatcher, adapters = setup(tmp_path)
    await dispatcher.startup()
    app = create_app(chat_service=service, chat_dispatcher=dispatcher)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        room = (await client.post("/api/v1/chats", json={
            "title": "只读讨论", "idempotency_key": str(uuid4()),
        })).json()
        room_id = room["room_id"]
        payload = {"content": "@白金 请和队友讨论输入校验，不改文件", "idempotency_key": str(uuid4())}
        posted = await client.post(f"/api/v1/chats/{room_id}/messages", json=payload)
        assert posted.status_code == 201
        assert posted.json()["discussion_queued"] is True
        assert posted.json()["execution_authorized"] is False
        assert posted.json()["agent_dispatched"] is False
        await dispatcher.wait_idle()
        turns = (await client.get(f"/api/v1/chats/{room_id}/turns")).json()["items"]
        assert [item["status"] for item in turns] == ["succeeded"] * 3
        messages = (await client.get(f"/api/v1/chats/{room_id}/messages")).json()["items"]
        assert len(messages) == 4
        assert [message["message"]["content"] for message in messages[1:]] == [
            "白金建议先确定输入类型", "月见建议覆盖空值和 Unicode", "鲸鲸建议加入回归用例",
        ]
        assert {message["message"]["correlation_id"] for message in messages} == {
            messages[0]["message"]["correlation_id"]
        }
        planner_context = prompt_context(adapters[MemberRole.PLANNER].requests[0].prompt)
        implementer_context = prompt_context(adapters[MemberRole.IMPLEMENTER].requests[0].prompt)
        reviewer_context = prompt_context(adapters[MemberRole.REVIEWER].requests[0].prompt)
        assert planner_context["history"] == []
        assert planner_context["scope"] == "none"
        assert implementer_context["scope"] == "same_discussion"
        assert [item["excerpt"] for item in implementer_context["history"]] == [
            payload["content"]
        ]
        assert [item["role"] for item in reviewer_context["history"]] == [
            "human", "planner"
        ]
        assert reviewer_context["reply_to"] == messages[1]["message"]["message_id"]
        assert "发送者：月见（implementer）" in adapters[MemberRole.REVIEWER].requests[0].prompt
        await client.post(f"/api/v1/chats/{room_id}/messages", json=payload)
        await dispatcher.wait_idle()
        assert len((await client.get(f"/api/v1/chats/{room_id}/messages")).json()["items"]) == 4
        for role in (MemberRole.PLANNER, MemberRole.IMPLEMENTER, MemberRole.REVIEWER):
            request = adapters[role].requests[0]
            assert request.discussion_only and request.standalone_chat_room_id == UUID(room_id)
            assert not (request.working_directory / ".git").exists()
            assert not request.working_directory.exists()  # terminal cleanup
    with service.store.database.connect() as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'tasks'"
        ).fetchone() is None


@pytest.mark.asyncio
async def test_context_is_bounded_persisted_and_room_scoped(tmp_path: Path) -> None:
    planner = FakeAgentAdapter(FakeAgentScenario(output={
        "message": '{"content":"已了解","handoff_to":[]}',
    }))
    service, dispatcher, _ = setup(tmp_path, planner=planner)
    await dispatcher.startup()
    room = service.create_room(title="当前房间", idempotency_key=uuid4())
    other = service.create_room(title="其它房间", idempotency_key=uuid4())
    service.post_message(
        other.room_id, content="@白金 PRIVATE_OTHER_ROOM", idempotency_key=uuid4(),
        reply_to=None,
    )
    for index in range(8):
        content = f"@白金 历史消息{index} " + ("x" * 600)
        if index == 7:
            content = '@白金 历史消息7 "quoted"\n第二行 ' + ("x" * 600)
        service.post_message(
            room.room_id, content=content,
            idempotency_key=uuid4(), reply_to=None,
        )

    reopened, restarted, adapters = setup(tmp_path, planner=FakeAgentAdapter(
        FakeAgentScenario(output={"message": '{"content":"收到","handoff_to":[]}'}),
    ))
    await restarted.startup()
    current = reopened.post_message(
        room.room_id, content="@白金 新话题", idempotency_key=uuid4(), reply_to=None,
    )
    restarted.enqueue(current)
    await restarted.wait_idle()
    prompt = adapters[MemberRole.PLANNER].requests[0].prompt
    context = prompt_context(prompt)
    assert context["scope"] == "room_recent_other_discussions"
    assert context["room_title"] == "当前房间"
    assert [item["sequence"] for item in context["history"]] == [7, 8, 9]
    assert all(len(item["excerpt"]) <= 241 for item in context["history"])
    assert "PRIVATE_OTHER_ROOM" not in prompt
    assert "历史消息0" not in prompt
    assert len(prompt) < 5000


@pytest.mark.asyncio
async def test_direct_reply_keeps_old_parent_reference_within_context_window(
    tmp_path: Path,
) -> None:
    planner = FakeAgentAdapter(FakeAgentScenario(output={
        "message": '{"content":"先确认边界","handoff_to":[]}',
    }))
    service, dispatcher, adapters = setup(tmp_path, planner=planner)
    await dispatcher.startup()
    room = service.create_room(title="关联追问", idempotency_key=uuid4())
    root = service.post_message(
        room.room_id, content="@白金 最初需求", idempotency_key=uuid4(), reply_to=None,
    )
    dispatcher.enqueue(root)
    await dispatcher.wait_idle()
    parent = service.store.list_messages(room.room_id)[1]
    for index in range(8):
        service.post_message(
            room.room_id, content=f"中间追问{index}", idempotency_key=uuid4(),
            reply_to=parent.message.message_id,
        )
    current = service.post_message(
        room.room_id, content="请接着最初的回答说", idempotency_key=uuid4(),
        reply_to=parent.message.message_id,
    )
    dispatcher.enqueue(current)
    await dispatcher.wait_idle()
    context = prompt_context(adapters[MemberRole.PLANNER].requests[-1].prompt)
    assert context["scope"] == "same_discussion"
    assert len(context["history"]) == 6
    assert context["history"][0]["message_id"] == str(parent.message.message_id)
    assert context["history"][0]["excerpt"] == "先确认边界"
    assert context["reply_to"] == str(parent.message.message_id)
    assert all(item["message_id"] != str(current.message.message_id)
               for item in context["history"])


@pytest.mark.asyncio
async def test_multi_mention_answers_once_per_agent_without_repeated_handoffs(
    tmp_path: Path,
) -> None:
    service, dispatcher, adapters = setup(tmp_path)
    await dispatcher.startup()
    room = service.create_room(title="三人讨论", idempotency_key=uuid4())
    original = service.post_message(
        room.room_id, content="@白金 @月见 @鲸鲸 各自说说风险",
        idempotency_key=uuid4(), reply_to=None,
    )
    assert len(dispatcher.enqueue(original)) == 3
    await dispatcher.wait_idle()

    turns = service.store.list_turns(room.room_id)
    messages = service.store.list_messages(room.room_id)
    assert len(turns) == 3
    assert all(turn.status is ChatTurnStatus.SUCCEEDED for turn in turns)
    assert len(messages) == 4
    assert all(len(adapter.requests) == 1 for adapter in adapters.values())
    human_id = next(member.member_id for member in room.members if member.role is MemberRole.HUMAN)
    assert all(message.message.recipient_ids == (human_id,) for message in messages[1:])

    # A deliberate Human follow-up is not an automatic Agent-to-Agent retry.
    planner_id = next(member.member_id for member in room.members
                      if member.role is MemberRole.PLANNER)
    planner_reply = next(message for message in messages[1:]
                         if message.message.sender_id == planner_id)
    follow_up = service.post_message(
        room.room_id, content="请进一步解释", idempotency_key=uuid4(),
        reply_to=planner_reply.message.message_id,
    )
    assert len(dispatcher.enqueue(follow_up)) == 1
    await dispatcher.wait_idle()
    assert len(adapters[MemberRole.PLANNER].requests) == 2
    assert len(service.store.list_turns(room.room_id)) == 4


@pytest.mark.asyncio
async def test_two_agents_handoff_to_same_teammate_only_runs_once(tmp_path: Path) -> None:
    invite_reviewer = FakeAgentAdapter(FakeAgentScenario(output={
        "message": '{"content":"请鲸鲸补充","handoff_to":["reviewer"]}',
    }))
    service, dispatcher, adapters = setup(
        tmp_path, planner=invite_reviewer,
        implementer=FakeAgentAdapter(FakeAgentScenario(output={
            "message": '{"content":"也请鲸鲸补充","handoff_to":["reviewer"]}',
        })),
    )
    await dispatcher.startup()
    room = service.create_room(title="并发接话", idempotency_key=uuid4())
    original = service.post_message(
        room.room_id, content="@白金 @月见 请讨论", idempotency_key=uuid4(), reply_to=None,
    )
    assert len(dispatcher.enqueue(original)) == 2
    await dispatcher.wait_idle()
    turns = service.store.list_turns(room.room_id)
    assert len(turns) == 3
    assert all(turn.status is ChatTurnStatus.SUCCEEDED for turn in turns)
    assert len(adapters[MemberRole.REVIEWER].requests) == 1
    assert len(service.store.list_messages(room.room_id)) == 4
    reviewer_id = next(member.member_id for member in room.members
                       if member.role is MemberRole.REVIEWER)
    assert reviewer_id in service.store.addressed_agents(room.room_id, original.message.correlation_id)
    assert not service.store.pending_for(reviewer_id, correlation_id=original.message.correlation_id)


def test_persisted_claim_blocks_late_agent_handoff_and_acks_delivery(tmp_path: Path) -> None:
    service, _, _ = setup(tmp_path)
    room = service.create_room(title="重复转交", idempotency_key=uuid4())
    original = service.post_message(
        room.room_id, content="@白金 @鲸鲸 各自讨论", idempotency_key=uuid4(), reply_to=None,
    )
    by_role = {member.role: member.member_id for member in room.members}
    for role in (MemberRole.PLANNER, MemberRole.REVIEWER):
        turn, created = service.store.claim_turn(
            original.message.message_id, by_role[role], max_turns=6,
        )
        assert created and turn is not None
    late = service.store.append_message(StandaloneChatMessage(
        room_id=room.room_id, trace_id=room.trace_id,
        sender_id=by_role[MemberRole.PLANNER],
        recipient_ids=(by_role[MemberRole.HUMAN], by_role[MemberRole.REVIEWER]),
        content="还请鲸鲸再说一次", reply_to=original.message.message_id,
        causation_id=original.message.message_id,
        correlation_id=original.message.correlation_id,
        idempotency_key="late-handoff",
    ))
    assert service.store.claim_turn(
        late.message.message_id, by_role[MemberRole.REVIEWER], max_turns=6,
    ) == (None, False)
    assert service.store.claim_turn(
        late.message.message_id, by_role[MemberRole.REVIEWER], max_turns=6,
    ) == (None, False)
    assert service.store.get_message(late.message.message_id).deliveries[1].status.value == (
        "acknowledged"
    )
    assert len(service.store.list_turns(room.room_id)) == 2


@pytest.mark.asyncio
async def test_budget_stops_teammate_loop_and_acknowledges_skipped_delivery(tmp_path: Path) -> None:
    service, dispatcher, adapters = setup(tmp_path, max_turns=1)
    await dispatcher.startup()
    room = service.create_room(title="边界", idempotency_key=uuid4())
    stored = service.post_message(
        room.room_id, content="@白金 @鲸鲸 请讨论", idempotency_key=uuid4(), reply_to=None,
    )
    dispatcher.enqueue(stored)
    await dispatcher.wait_idle()
    turns = service.store.list_turns(room.room_id)
    assert {turn.status for turn in turns} == {
        ChatTurnStatus.SUCCEEDED, ChatTurnStatus.BUDGET_EXHAUSTED,
    }
    assert len(adapters[MemberRole.PLANNER].requests) == 1
    assert not adapters[MemberRole.IMPLEMENTER].requests
    assert not adapters[MemberRole.REVIEWER].requests


@pytest.mark.asyncio
async def test_failed_and_invalid_outputs_are_not_retried(tmp_path: Path) -> None:
    planner = FakeAgentAdapter(FakeAgentScenario(start_error="provider unavailable"))
    service, dispatcher, _ = setup(tmp_path, planner=planner)
    await dispatcher.startup()
    room = service.create_room(title="故障", idempotency_key=uuid4())
    stored = service.post_message(
        room.room_id, content="@白金 请分析", idempotency_key=uuid4(), reply_to=None,
    )
    dispatcher.enqueue(stored)
    await dispatcher.wait_idle()
    first_turn = service.store.list_turns(room.room_id)[0]
    assert first_turn.status is ChatTurnStatus.INTERRUPTED
    assert first_turn.error == "Agent adapter could not start; check CLI and sandbox configuration"
    assert "provider unavailable" not in first_turn.error
    dispatcher.enqueue(stored)
    await dispatcher.wait_idle()
    assert planner.requests == []
    assert len(service.store.list_messages(room.room_id)) == 1

    invalid = FakeAgentAdapter(FakeAgentScenario(output={
        "message": '{"content":"hello","handoff_to":["planner"]}',
    }))
    other, other_dispatcher, _ = setup(tmp_path / "other", planner=invalid)
    await other_dispatcher.startup()
    other_room = other.create_room(title="无效转交", idempotency_key=uuid4())
    other_dispatcher.enqueue(other.post_message(
        other_room.room_id, content="@白金 请分析", idempotency_key=uuid4(), reply_to=None,
    ))
    await other_dispatcher.wait_idle()
    invalid_turn = other.store.list_turns(other_room.room_id)[0]
    assert invalid_turn.status is ChatTurnStatus.FAILED
    assert invalid_turn.error == "Agent reply format was invalid"
    assert len(other.store.list_messages(other_room.room_id)) == 1


@pytest.mark.asyncio
async def test_missing_key_and_model_exit_have_safe_ui_errors(tmp_path: Path) -> None:
    missing_key = FakeAgentAdapter(FakeAgentScenario(
        start_error="KIMI_MODEL_API_KEY is required; secret details must not appear",
    ))
    service, dispatcher, _ = setup(tmp_path, planner=missing_key)
    await dispatcher.startup()
    room = service.create_room(title="配置错误", idempotency_key=uuid4())
    dispatcher.enqueue(service.post_message(
        room.room_id, content="@白金 请讨论", idempotency_key=uuid4(), reply_to=None,
    ))
    await dispatcher.wait_idle()
    error = service.store.list_turns(room.room_id)[0].error
    assert error == "KIMI_MODEL_API_KEY is missing in the server environment"
    assert "secret details" not in error

    failed = FakeAgentAdapter(FakeAgentScenario(reason=AgentExitReason.TIMED_OUT, exit_code=143))
    other, other_dispatcher, _ = setup(tmp_path / "other", planner=failed)
    await other_dispatcher.startup()
    other_room = other.create_room(title="超时", idempotency_key=uuid4())
    other_dispatcher.enqueue(other.post_message(
        other_room.room_id, content="@白金 请讨论", idempotency_key=uuid4(), reply_to=None,
    ))
    await other_dispatcher.wait_idle()
    timed_out = other.store.list_turns(other_room.room_id)[0]
    assert timed_out.status is ChatTurnStatus.FAILED
    assert timed_out.error == (
        "Agent process ended: timed_out, exit=143"
    )
    assert len(other.store.list_messages(other_room.room_id)) == 1


@pytest.mark.asyncio
async def test_cancel_running_turn_and_restart_fence_unknown_claim(tmp_path: Path) -> None:
    blocked = FakeAgentAdapter(FakeAgentScenario(block_until_cancel=True))
    service, dispatcher, _ = setup(tmp_path, planner=blocked)
    await dispatcher.startup()
    room = service.create_room(title="取消", idempotency_key=uuid4())
    stored = service.post_message(
        room.room_id, content="@白金 请分析", idempotency_key=uuid4(), reply_to=None,
    )
    claim = dispatcher.enqueue(stored)[0]
    for _ in range(100):
        if blocked.requests:
            break
        await asyncio.sleep(0.01)
    assert blocked.requests
    cancelled = await dispatcher.cancel(claim.turn_id)
    assert cancelled.status is ChatTurnStatus.CANCELLED
    assert len(service.store.list_messages(room.room_id)) == 1
    await dispatcher.shutdown()

    another = service.post_message(
        room.room_id, content="@鲸鲸 再看看", idempotency_key=uuid4(), reply_to=None,
    )
    reviewer = next(member for member in room.members if member.role is MemberRole.REVIEWER)
    pending, created = service.store.claim_turn(
        another.message.message_id, reviewer.member_id, max_turns=6,
    )
    assert created and pending.status is ChatTurnStatus.QUEUED
    reopened, restarted, adapters = setup(tmp_path)
    await restarted.startup()
    assert reopened.store.get_turn(pending.turn_id).status is ChatTurnStatus.INTERRUPTED

    running_message = service.post_message(
        room.room_id, content="@月见 请补充", idempotency_key=uuid4(), reply_to=None,
    )
    implementer = next(member for member in room.members if member.role is MemberRole.IMPLEMENTER)
    running, _ = service.store.claim_turn(
        running_message.message.message_id, implementer.member_id, max_turns=6,
    )
    service.store.transition_turn(
        running.turn_id, from_status=ChatTurnStatus.QUEUED,
        to_status=ChatTurnStatus.RUNNING, session_id=uuid4(),
    )
    await restarted.startup()
    assert reopened.store.get_turn(running.turn_id).status is ChatTurnStatus.INTERRUPTED
    restarted.enqueue(another)
    await restarted.wait_idle()
    assert not adapters[MemberRole.REVIEWER].requests
    assert reopened.store.get_turn(pending.turn_id).status is ChatTurnStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_http_cancel_is_scoped_to_its_room(tmp_path: Path) -> None:
    blocked = FakeAgentAdapter(FakeAgentScenario(block_until_cancel=True))
    service, dispatcher, _ = setup(tmp_path, planner=blocked)
    await dispatcher.startup()
    app = create_app(chat_service=service, chat_dispatcher=dispatcher)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rooms = []
        for title in ("one", "two"):
            response = await client.post("/api/v1/chats", json={
                "title": title, "idempotency_key": str(uuid4()),
            })
            rooms.append(response.json()["room_id"])
        post = await client.post(f"/api/v1/chats/{rooms[0]}/messages", json={
            "content": "@白金 请分析", "idempotency_key": str(uuid4()),
        })
        turn_id = post.json()["turns"][0]["turn_id"]
        for _ in range(100):
            if blocked.requests:
                break
            await asyncio.sleep(0.01)
        assert blocked.requests
        assert (await client.post(
            f"/api/v1/chats/{rooms[1]}/turns/{turn_id}/cancel"
        )).status_code == 404
        cancelled = await client.post(f"/api/v1/chats/{rooms[0]}/turns/{turn_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert (await client.get(f"/api/v1/chats/{rooms[0]}/turns")).json()["items"][0][
            "status"
        ] == "cancelled"


def test_fastapi_lifespan_starts_and_stops_chat_dispatcher(tmp_path: Path) -> None:
    service, dispatcher, _ = setup(tmp_path, max_turns=1)
    with TestClient(create_app(chat_service=service, chat_dispatcher=dispatcher)) as client:
        room_id = client.post("/api/v1/chats", json={
            "title": "生命周期", "idempotency_key": str(uuid4()),
        }).json()["room_id"]
        response = client.post(f"/api/v1/chats/{room_id}/messages", json={
            "content": "@白金 请讨论", "idempotency_key": str(uuid4()),
        })
        assert response.status_code == 201
        message_id = UUID(response.json()["message"]["message"]["message_id"])
        for _ in range(100):
            turn = client.get(f"/api/v1/chats/{room_id}/turns").json()["items"][0]
            if turn["status"] == "succeeded":
                break
            time.sleep(0.01)
        assert turn["status"] == "succeeded"
    assert dispatcher.enqueue(service.store.get_message(message_id)) == ()
