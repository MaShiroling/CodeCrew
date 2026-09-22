from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.team import (
    ChatMessage,
    MemberKind,
    MemberRole,
    MessageRecipient,
    MessageType,
    RecipientKind,
    RoomMember,
    RoomStatus,
    TeamRoom,
)


def test_room_members_and_message_form_a_task_scoped_conversation() -> None:
    room_id = uuid4()
    task_id = uuid4()
    trace_id = uuid4()
    planner = RoomMember(
        room_id=room_id,
        name="claude-planner",
        role=MemberRole.PLANNER,
        kind=MemberKind.AGENT,
    )
    implementer = RoomMember(
        room_id=room_id,
        name="codex-implementer",
        role=MemberRole.IMPLEMENTER,
        kind=MemberKind.AGENT,
    )
    room = TeamRoom(
        room_id=room_id,
        task_id=task_id,
        trace_id=trace_id,
        name="Implement issue 42",
        members=(planner, implementer),
    )
    message = ChatMessage(
        room_id=room_id,
        task_id=task_id,
        trace_id=trace_id,
        sender_id=implementer.member_id,
        recipients=(
            MessageRecipient(kind=RecipientKind.MEMBER, member_id=planner.member_id),
        ),
        type=MessageType.QUESTION,
        content="What should happen when the configuration is absent?",
        idempotency_key="question-1",
    )

    assert room.status is RoomStatus.ACTIVE
    assert message.recipients[0].member_id == planner.member_id


@pytest.mark.parametrize(
    ("role", "kind"),
    [
        (MemberRole.PLANNER, MemberKind.SYSTEM),
        (MemberRole.VERIFIER, MemberKind.AGENT),
        (MemberRole.HUMAN, MemberKind.AGENT),
    ],
)
def test_member_role_identity_mismatch_is_rejected(role, kind) -> None:
    with pytest.raises(ValidationError, match="must"):
        RoomMember(room_id=uuid4(), name="invalid", role=role, kind=kind)


def test_recipient_requires_exact_target_shape() -> None:
    with pytest.raises(ValidationError, match="member recipient"):
        MessageRecipient(kind=RecipientKind.MEMBER)
    with pytest.raises(ValidationError, match="room recipient"):
        MessageRecipient(
            kind=RecipientKind.ROOM,
            role=MemberRole.PLANNER,
        )


def test_message_rejects_duplicate_recipients_and_self_reply() -> None:
    recipient = MessageRecipient(kind=RecipientKind.ROLE, role=MemberRole.REVIEWER)
    values = {
        "room_id": uuid4(),
        "task_id": uuid4(),
        "trace_id": uuid4(),
        "sender_id": uuid4(),
        "recipients": (recipient, recipient),
        "type": MessageType.MESSAGE,
        "content": "Please inspect the patch",
        "idempotency_key": "message-1",
    }
    with pytest.raises(ValidationError, match="recipients must be unique"):
        ChatMessage(**values)

    message_id = uuid4()
    values["recipients"] = (recipient,)
    values["message_id"] = message_id
    values["reply_to"] = message_id
    with pytest.raises(ValidationError, match="reply to itself"):
        ChatMessage(**values)


def test_room_rejects_foreign_member() -> None:
    with pytest.raises(ValidationError, match="belong to the room"):
        TeamRoom(
            task_id=uuid4(),
            trace_id=uuid4(),
            name="room",
            members=(
                RoomMember(
                    room_id=uuid4(),
                    name="planner",
                    role=MemberRole.PLANNER,
                    kind=MemberKind.AGENT,
                ),
            ),
        )
