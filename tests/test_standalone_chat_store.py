"""Standalone chat data must remain independent of coding tasks and repositories."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.chat import (
    StandaloneChatConflictError,
    StandaloneChatIdempotencyError,
    StandaloneChatMemberNotFoundError,
    StandaloneChatMessage,
    StandaloneChatMessageNotFoundError,
    StandaloneChatRoom,
    StandaloneChatStore,
)
from app.storage import SQLiteDatabase, TaskRepository
from app.team.models import MemberKind, MemberRole, MessageDeliveryStatus, RoomMember, RoomStatus


def make_room(title: str = "输入校验讨论") -> StandaloneChatRoom:
    room_id = uuid4()
    members = tuple(
        RoomMember(room_id=room_id, name=name, role=role, kind=kind)
        for name, role, kind in (
            ("human", MemberRole.HUMAN, MemberKind.HUMAN),
            ("白金", MemberRole.PLANNER, MemberKind.AGENT),
            ("月见", MemberRole.IMPLEMENTER, MemberKind.AGENT),
            ("鲸鲸", MemberRole.REVIEWER, MemberKind.AGENT),
        )
    )
    return StandaloneChatRoom(room_id=room_id, title=title, members=members)


def make_message(room: StandaloneChatRoom, content: str = "请讨论输入校验") -> StandaloneChatMessage:
    return StandaloneChatMessage(
        room_id=room.room_id,
        trace_id=room.trace_id,
        sender_id=room.members[0].member_id,
        recipient_ids=tuple(member.member_id for member in room.members[1:]),
        content=content,
        idempotency_key=str(uuid4()),
    )


def make_store(tmp_path: Path) -> StandaloneChatStore:
    store = StandaloneChatStore(SQLiteDatabase(tmp_path / "chat.sqlite3"))
    store.initialize()
    return store


def test_room_and_messages_survive_restart_without_task_or_repository(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    room = make_room()
    assert store.create_room(room) == room
    original = make_message(room)
    stored = store.append_message(original)

    reopened = make_store(tmp_path)
    assert reopened.get_room(room.room_id) == room
    assert reopened.list_rooms() == (room,)
    assert reopened.list_messages(room.room_id) == (stored,)
    assert {delivery.status for delivery in stored.deliveries} == {
        MessageDeliveryStatus.PENDING
    }
    assert not hasattr(room, "task_id")
    assert not hasattr(room, "repository_path")
    assert not hasattr(original, "task_id")
    with reopened.database.connect() as connection:
        tables = {row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
    assert "standalone_chat_rooms" in tables
    assert "tasks" not in tables
    assert "workflow_runtime_contexts" not in tables


def test_chat_migration_coexists_with_existing_task_storage(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "shared.sqlite3")
    TaskRepository(database).initialize()
    store = StandaloneChatStore(database)
    store.initialize()
    room = make_room()
    store.create_room(room)

    assert database.schema_version == 15
    assert store.get_room(room.room_id) == room
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM standalone_chat_rooms").fetchone()[0] == 1


def test_room_creation_replay_and_conflict(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    room = make_room()
    assert store.create_room(room) == room
    assert store.create_room(room) == room
    with pytest.raises(StandaloneChatConflictError):
        store.create_room(room.model_copy(update={"title": "different"}))
    with pytest.raises(StandaloneChatConflictError):
        store.create_room(make_room().model_copy(update={"trace_id": room.trace_id}))
    assert store.list_rooms() == (room,)


def test_directed_delivery_ack_and_correlated_reply(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    room = make_room()
    store.create_room(room)
    initial = store.append_message(make_message(room))
    planner = room.members[1]
    assert store.pending_for(planner.member_id) == (initial,)

    acked = store.acknowledge(initial.message.message_id, recipient_id=planner.member_id)
    assert acked.deliveries[0].status is MessageDeliveryStatus.ACKNOWLEDGED
    assert acked.deliveries[0].acknowledged_at is not None
    assert store.acknowledge(initial.message.message_id, recipient_id=planner.member_id) == acked
    assert not store.pending_for(planner.member_id)
    assert len(store.pending_for(room.members[2].member_id)) == 1

    reply = StandaloneChatMessage(
        room_id=room.room_id,
        trace_id=room.trace_id,
        sender_id=planner.member_id,
        recipient_ids=(room.members[0].member_id,),
        content="需要覆盖 None、空白和 Unicode 输入",
        reply_to=initial.message.message_id,
        causation_id=initial.message.message_id,
        correlation_id=initial.message.correlation_id,
        idempotency_key=str(uuid4()),
    )
    stored_reply = store.append_message(reply)
    assert store.pending_for(
        room.members[0].member_id, correlation_id=initial.message.correlation_id
    ) == (stored_reply,)
    assert store.list_messages(room.room_id, after_sequence=initial.sequence) == (stored_reply,)


def test_idempotent_replay_and_conflicting_reuse(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    room = make_room()
    store.create_room(room)
    message = make_message(room)
    first = store.append_message(message)
    retry = message.model_copy(update={"message_id": uuid4()})
    assert store.append_message(retry) == first
    with pytest.raises(StandaloneChatIdempotencyError):
        store.append_message(retry.model_copy(update={"content": "different"}))
    assert len(store.list_messages(room.room_id)) == 1


def test_concurrent_same_key_persists_once(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    room = make_room()
    store.create_room(room)
    message = make_message(room)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(store.append_message, (
            message.model_copy(update={"message_id": uuid4()}) for _ in range(8)
        )))
    assert len({result.message.message_id for result in results}) == 1
    assert len(store.list_messages(room.room_id)) == 1


def test_room_scope_close_and_invalid_members_fail_closed(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    room = make_room()
    other = make_room("另一间聊天室")
    store.create_room(room)
    store.create_room(other)
    first = store.append_message(make_message(room))

    foreign_reply = make_message(other).model_copy(update={
        "reply_to": first.message.message_id,
        "correlation_id": first.message.correlation_id,
    })
    with pytest.raises(StandaloneChatMessageNotFoundError):
        store.append_message(foreign_reply)
    with pytest.raises(StandaloneChatMemberNotFoundError):
        store.append_message(make_message(room).model_copy(update={"sender_id": uuid4()}))
    with pytest.raises(StandaloneChatConflictError, match="trace"):
        store.append_message(make_message(room).model_copy(update={"trace_id": uuid4()}))

    closed = store.close_room(room.room_id)
    assert closed.status is RoomStatus.CLOSED
    assert store.close_room(room.room_id) == closed
    assert store.append_message(first.message) == first  # Safe retry after close.
    with pytest.raises(StandaloneChatConflictError, match="closed"):
        store.append_message(make_message(room))
    assert len(store.list_messages(room.room_id)) == 1


def test_contract_rejects_task_fields_blank_content_and_duplicate_recipients() -> None:
    room = make_room()
    with pytest.raises(ValidationError):
        StandaloneChatRoom.model_validate({
            **room.model_dump(), "repository_path": "/some/repo",
        })
    with pytest.raises(ValidationError):
        StandaloneChatRoom.model_validate({
            **room.model_dump(), "members": room.members[:3],
        })
    with pytest.raises(ValidationError):
        StandaloneChatMessage.model_validate({
            **make_message(room).model_dump(), "content": "  ",
        })
    with pytest.raises(ValidationError):
        StandaloneChatMessage.model_validate({
            **make_message(room).model_dump(),
            "recipient_ids": (room.members[1].member_id, room.members[1].member_id),
        })


def test_store_revalidates_models_copied_with_unchecked_updates(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    room = make_room()
    with pytest.raises(ValidationError):
        store.create_room(room.model_copy(update={"members": room.members[:3]}))
    store.create_room(room)
    with pytest.raises(ValidationError):
        store.append_message(make_message(room).model_copy(update={"recipient_ids": ()}))
    assert not store.list_messages(room.room_id)
