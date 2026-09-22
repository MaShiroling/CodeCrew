from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from app.storage import ArtifactReference, ArtifactType, SQLiteDatabase
from app.team import (
    ChatIdempotencyConflictError,
    ChatMessage,
    ChatMessageNotFoundError,
    InvalidChatAcknowledgementError,
    MemberKind,
    MemberNotFoundError,
    MemberRole,
    MessageDeliveryStatus,
    MessageRecipient,
    MessageType,
    RecipientKind,
    RoomConflictError,
    RoomMember,
    RoomStatus,
    TeamRoom,
    TeamRoomStore,
)


def make_context(tmp_path: Path):
    store = TeamRoomStore(SQLiteDatabase(tmp_path / "codecrew.sqlite3"))
    store.initialize()
    room_id = uuid4()
    planner = RoomMember(
        room_id=room_id,
        name="planner",
        role=MemberRole.PLANNER,
        kind=MemberKind.AGENT,
    )
    implementer = RoomMember(
        room_id=room_id,
        name="implementer",
        role=MemberRole.IMPLEMENTER,
        kind=MemberKind.AGENT,
    )
    reviewer = RoomMember(
        room_id=room_id,
        name="reviewer",
        role=MemberRole.REVIEWER,
        kind=MemberKind.AGENT,
    )
    room = TeamRoom(
        room_id=room_id,
        task_id=uuid4(),
        trace_id=uuid4(),
        name="Task room",
        members=(planner, implementer, reviewer),
    )
    store.create_room(room)
    return store, room, planner, implementer, reviewer


def message(room, sender, recipient, **updates):
    values = {
        "room_id": room.room_id,
        "task_id": room.task_id,
        "trace_id": room.trace_id,
        "sender_id": sender.member_id,
        "recipients": (
            MessageRecipient(kind=RecipientKind.MEMBER, member_id=recipient.member_id),
        ),
        "type": MessageType.QUESTION,
        "content": "Please clarify the expected behavior",
        "idempotency_key": f"message-{uuid4()}",
    }
    values.update(updates)
    return ChatMessage(**values)


def test_room_and_members_persist_across_restart(tmp_path: Path) -> None:
    store, room, *_ = make_context(tmp_path)
    reopened = TeamRoomStore(SQLiteDatabase(store.database.path))
    reopened.initialize()

    assert reopened.get_room(room.room_id) == room
    assert reopened.database.schema_version == 5


def test_plan_revisions_form_an_immutable_clarification_chain(tmp_path: Path) -> None:
    store, room, planner, implementer, _ = make_context(tmp_path)
    first_artifact = ArtifactReference(
        artifact_id=uuid4(),
        type=ArtifactType.PLAN,
        sha256="a" * 64,
        summary="initial plan",
    )
    first = store.append_message(
        message(
            room,
            planner,
            implementer,
            type=MessageType.PLAN_SHARED,
            artifacts=(first_artifact,),
        ),
        recipient_ids=(implementer.member_id,),
    )
    question = store.append_message(
        message(room, implementer, planner),
        recipient_ids=(planner.member_id,),
    )
    second_artifact = ArtifactReference(
        artifact_id=uuid4(),
        type=ArtifactType.PLAN,
        sha256="b" * 64,
        summary="revised plan",
    )
    second = store.append_message(
        message(
            room,
            planner,
            implementer,
            type=MessageType.PLAN_SHARED,
            artifacts=(second_artifact,),
            supersedes_artifact_id=first_artifact.artifact_id,
            addresses_message_ids=(question.message.message_id,),
        ),
        recipient_ids=(implementer.member_id,),
    )

    revisions = store.list_plan_revisions(room.room_id)

    assert [revision.version for revision in revisions] == [1, 2]
    assert revisions[0].message_id == first.message.message_id
    assert revisions[1].message_id == second.message.message_id
    assert revisions[1].supersedes_artifact_id == first_artifact.artifact_id
    assert revisions[1].addresses_message_ids == (question.message.message_id,)
    assert store.latest_plan_revision(room.room_id) == revisions[1]


def test_revised_plan_must_supersede_latest_and_address_a_question(
    tmp_path: Path,
) -> None:
    store, room, planner, implementer, _ = make_context(tmp_path)
    first_artifact = ArtifactReference(
        artifact_id=uuid4(), type=ArtifactType.PLAN, sha256="a" * 64, summary="v1"
    )
    store.append_message(
        message(
            room,
            planner,
            implementer,
            type=MessageType.PLAN_SHARED,
            artifacts=(first_artifact,),
        ),
        recipient_ids=(implementer.member_id,),
    )
    invalid_artifact = ArtifactReference(
        artifact_id=uuid4(), type=ArtifactType.PLAN, sha256="b" * 64, summary="v2"
    )

    with pytest.raises(RoomConflictError, match="must address"):
        store.append_message(
            message(
                room,
                planner,
                implementer,
                type=MessageType.PLAN_SHARED,
                artifacts=(invalid_artifact,),
                supersedes_artifact_id=first_artifact.artifact_id,
            ),
            recipient_ids=(implementer.member_id,),
        )


def test_incremental_message_read_and_per_recipient_ack(tmp_path: Path) -> None:
    store, room, planner, implementer, reviewer = make_context(tmp_path)
    first = store.append_message(
        message(room, implementer, planner),
        recipient_ids=(planner.member_id, reviewer.member_id),
    )
    second = store.append_message(
        message(room, planner, implementer, type=MessageType.ANSWER),
        recipient_ids=(implementer.member_id,),
    )

    assert store.list_messages(room.room_id, after_sequence=first.sequence) == (second,)
    assert store.pending_for(planner.member_id) == (first,)
    acknowledged = store.acknowledge(
        first.message.message_id, recipient_id=planner.member_id
    )
    assert acknowledged.status is MessageDeliveryStatus.ACKNOWLEDGED
    assert acknowledged.acknowledged_at is not None
    assert store.pending_for(planner.member_id) == ()
    assert store.pending_for(reviewer.member_id)[0].message == first.message
    assert store.acknowledge(
        first.message.message_id, recipient_id=planner.member_id
    ) == acknowledged
    with pytest.raises(InvalidChatAcknowledgementError):
        store.acknowledge(first.message.message_id, recipient_id=implementer.member_id)


def test_message_write_is_idempotent_and_conflicts_are_rejected(tmp_path: Path) -> None:
    store, room, planner, implementer, _ = make_context(tmp_path)
    original = message(
        room,
        implementer,
        planner,
        idempotency_key="stable-question",
    )

    first = store.append_message(original, recipient_ids=(planner.member_id,))
    repeated = store.append_message(
        original.model_copy(update={"message_id": uuid4()}),
        recipient_ids=(planner.member_id,),
    )

    assert repeated == first
    with pytest.raises(ChatIdempotencyConflictError, match="different chat content"):
        store.append_message(
            original.model_copy(
                update={"message_id": uuid4(), "content": "Different question"}
            ),
            recipient_ids=(planner.member_id,),
        )


def test_reply_thread_returns_root_and_all_descendants(tmp_path: Path) -> None:
    store, room, planner, implementer, reviewer = make_context(tmp_path)
    root = store.append_message(
        message(room, implementer, planner), recipient_ids=(planner.member_id,)
    )
    answer = store.append_message(
        message(
            room,
            planner,
            implementer,
            type=MessageType.ANSWER,
            reply_to=root.message.message_id,
        ),
        recipient_ids=(implementer.member_id,),
    )
    comment = store.append_message(
        message(
            room,
            reviewer,
            implementer,
            type=MessageType.MESSAGE,
            reply_to=root.message.message_id,
        ),
        recipient_ids=(implementer.member_id,),
    )

    assert store.get_thread(answer.message.message_id) == (root, answer, comment)
    foreign = message(
        room,
        implementer,
        planner,
        reply_to=uuid4(),
    )
    with pytest.raises(ChatMessageNotFoundError, match="not in this room"):
        store.append_message(foreign, recipient_ids=(planner.member_id,))


def test_closed_room_rejects_members_and_messages(tmp_path: Path) -> None:
    store, room, planner, implementer, _ = make_context(tmp_path)
    closed = store.close_room(room.room_id)

    assert closed.status is RoomStatus.CLOSED
    assert closed.closed_at is not None
    with pytest.raises(RoomConflictError, match="closed room"):
        store.append_message(
            message(room, implementer, planner), recipient_ids=(planner.member_id,)
        )
    with pytest.raises(RoomConflictError, match="closed room"):
        store.add_member(
            RoomMember(
                room_id=room.room_id,
                name="new-reviewer",
                role=MemberRole.REVIEWER,
                kind=MemberKind.AGENT,
            )
        )


def test_message_rejects_foreign_task_sender_and_recipient(tmp_path: Path) -> None:
    store, room, planner, implementer, _ = make_context(tmp_path)
    foreign_id = uuid4()

    with pytest.raises(RoomConflictError, match="task or trace"):
        store.append_message(
            message(room, implementer, planner, task_id=uuid4()),
            recipient_ids=(planner.member_id,),
        )
    with pytest.raises(ValueError, match="resolved recipient"):
        store.append_message(
            message(room, implementer, planner), recipient_ids=()
        )
    with pytest.raises(MemberNotFoundError, match="recipient is not a room member"):
        store.append_message(
            message(room, implementer, planner), recipient_ids=(foreign_id,)
        )


def test_concurrent_writers_preserve_all_messages(tmp_path: Path) -> None:
    store, room, planner, implementer, _ = make_context(tmp_path)

    def write(index: int):
        return store.append_message(
            message(
                room,
                implementer,
                planner,
                content=f"Question {index}",
                idempotency_key=f"concurrent-{index}",
            ),
            recipient_ids=(planner.member_id,),
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        written = tuple(executor.map(write, range(20)))

    persisted = store.list_messages(room.room_id)
    assert len(written) == len(persisted) == 20
    assert len({item.sequence for item in persisted}) == 20
