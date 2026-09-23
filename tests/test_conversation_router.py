from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.storage import ArtifactReference, ArtifactStore, ArtifactType, SQLiteDatabase
from app.team import (
    ChatMessage,
    ConversationArtifactError,
    ConversationRouter,
    MemberKind,
    MemberRole,
    MessageRecipient,
    MessageType,
    RecipientKind,
    RoomMember,
    RouteNotAllowedError,
    SenderAuthenticationError,
    TeamRoom,
    TeamRoomStore,
)
from app.trace import TraceEventType


def make_context(tmp_path: Path):
    database = SQLiteDatabase(tmp_path / "codecrew.sqlite3")
    artifacts = ArtifactStore(database, tmp_path / "artifacts")
    rooms = TeamRoomStore(database)
    artifacts.initialize()
    rooms.initialize()
    room_id = uuid4()

    def member(name: str, role: MemberRole, kind: MemberKind) -> RoomMember:
        return RoomMember(room_id=room_id, name=name, role=role, kind=kind)

    members = {
        MemberRole.PLANNER: member("planner", MemberRole.PLANNER, MemberKind.AGENT),
        MemberRole.IMPLEMENTER: member(
            "implementer", MemberRole.IMPLEMENTER, MemberKind.AGENT
        ),
        MemberRole.REVIEWER: member("reviewer", MemberRole.REVIEWER, MemberKind.AGENT),
        MemberRole.VERIFIER: member("verifier", MemberRole.VERIFIER, MemberKind.SYSTEM),
        MemberRole.ORCHESTRATOR: member(
            "orchestrator", MemberRole.ORCHESTRATOR, MemberKind.SYSTEM
        ),
        MemberRole.HUMAN: member("human", MemberRole.HUMAN, MemberKind.HUMAN),
    }
    room = TeamRoom(
        room_id=room_id,
        task_id=uuid4(),
        trace_id=uuid4(),
        name="Coding team",
        members=tuple(members.values()),
    )
    rooms.create_room(room)
    return ConversationRouter(rooms, artifacts), room, members, artifacts


def direct_message(room, sender, recipient, **updates) -> ChatMessage:
    values = {
        "room_id": room.room_id,
        "task_id": room.task_id,
        "trace_id": room.trace_id,
        "sender_id": sender.member_id,
        "recipients": (
            MessageRecipient(kind=RecipientKind.MEMBER, member_id=recipient.member_id),
        ),
        "type": MessageType.MESSAGE,
        "content": "Coordinate this coding task",
        "idempotency_key": f"route-{uuid4()}",
    }
    values.update(updates)
    return ChatMessage(**values)


def put_artifact(
    artifacts: ArtifactStore,
    room: TeamRoom,
    *,
    task_id: UUID | None = None,
) -> ArtifactReference:
    metadata = artifacts.put_json(
        {"result": "evidence"},
        task_id=task_id or room.task_id,
        trace_id=room.trace_id,
        type=ArtifactType.GENERIC,
        created_by="test",
        filename="evidence.json",
    )
    return ArtifactReference.from_metadata(metadata, summary="evidence")


def test_planner_and_implementer_can_exchange_direct_messages(tmp_path: Path) -> None:
    router, room, members, _ = make_context(tmp_path)
    planner = members[MemberRole.PLANNER]
    implementer = members[MemberRole.IMPLEMENTER]
    outgoing = direct_message(room, planner, implementer)

    routed = router.route(outgoing, authenticated_sender_id=planner.member_id)

    assert routed.message == outgoing
    assert [delivery.recipient_id for delivery in routed.deliveries] == [
        implementer.member_id
    ]
    trace = router.trace_store.list(
        trace_id=room.trace_id,
        type=TraceEventType.CHAT_MESSAGE_PERSISTED,
    )
    assert trace[0].event.payload["message_id"] == str(outgoing.message_id)


def test_idempotent_message_retry_does_not_duplicate_trace_event(tmp_path: Path) -> None:
    router, room, members, _ = make_context(tmp_path)
    planner = members[MemberRole.PLANNER]
    implementer = members[MemberRole.IMPLEMENTER]
    outgoing = direct_message(
        room,
        planner,
        implementer,
        idempotency_key="stable-route",
    )

    first = router.route(outgoing, authenticated_sender_id=planner.member_id)
    repeated = router.route(
        outgoing.model_copy(update={"message_id": uuid4()}),
        authenticated_sender_id=planner.member_id,
    )

    assert repeated == first
    trace = router.trace_store.list(
        trace_id=room.trace_id,
        type=TraceEventType.CHAT_MESSAGE_PERSISTED,
    )
    assert len(trace) == 1
    assert trace[0].event.payload["message_id"] == str(first.message.message_id)


def test_role_and_room_targets_resolve_to_authorized_members(tmp_path: Path) -> None:
    router, room, members, _ = make_context(tmp_path)
    verifier = members[MemberRole.VERIFIER]
    reviewer = members[MemberRole.REVIEWER]
    by_role = direct_message(
        room,
        verifier,
        reviewer,
        recipients=(
            MessageRecipient(kind=RecipientKind.ROLE, role=MemberRole.REVIEWER),
        ),
        type=MessageType.STATUS_UPDATE,
    )
    routed = router.route(by_role, authenticated_sender_id=verifier.member_id)
    assert {item.recipient_id for item in routed.deliveries} == {reviewer.member_id}

    orchestrator = members[MemberRole.ORCHESTRATOR]
    broadcast = direct_message(
        room,
        orchestrator,
        reviewer,
        recipients=(MessageRecipient(kind=RecipientKind.ROOM),),
        type=MessageType.SYSTEM_EVENT,
    )
    routed = router.route(broadcast, authenticated_sender_id=orchestrator.member_id)
    assert len(routed.deliveries) == len(members) - 1


def test_router_blocks_forbidden_routes_and_sender_spoofing(tmp_path: Path) -> None:
    router, room, members, _ = make_context(tmp_path)
    planner = members[MemberRole.PLANNER]
    reviewer = members[MemberRole.REVIEWER]
    implementer = members[MemberRole.IMPLEMENTER]

    with pytest.raises(RouteNotAllowedError, match="cannot message roles: reviewer"):
        router.route(
            direct_message(room, planner, reviewer),
            authenticated_sender_id=planner.member_id,
        )
    with pytest.raises(SenderAuthenticationError, match="does not match"):
        router.route(
            direct_message(room, planner, implementer),
            authenticated_sender_id=implementer.member_id,
        )
    with pytest.raises(RouteNotAllowedError, match="system identity"):
        router.route(
            direct_message(
                room,
                planner,
                implementer,
                type=MessageType.SYSTEM_EVENT,
            ),
            authenticated_sender_id=planner.member_id,
        )


def test_router_enforces_privileged_message_types(tmp_path: Path) -> None:
    router, room, members, _ = make_context(tmp_path)
    implementer = members[MemberRole.IMPLEMENTER]
    reviewer = members[MemberRole.REVIEWER]
    planner = members[MemberRole.PLANNER]

    with pytest.raises(RouteNotAllowedError, match="only a reviewer"):
        router.route(
            direct_message(
                room,
                implementer,
                reviewer,
                type=MessageType.REVIEW_COMMENT,
            ),
            authenticated_sender_id=implementer.member_id,
        )
    with pytest.raises(RouteNotAllowedError, match="target a human"):
        router.route(
            direct_message(
                room,
                implementer,
                planner,
                type=MessageType.HUMAN_INPUT_REQUEST,
            ),
            authenticated_sender_id=implementer.member_id,
        )
    with pytest.raises(RouteNotAllowedError, match="require an artifact"):
        router.route(
            direct_message(
                room,
                implementer,
                reviewer,
                type=MessageType.ARTIFACT_SHARED,
            ),
            authenticated_sender_id=implementer.member_id,
        )
    with pytest.raises(RouteNotAllowedError, match="review_report"):
        router.route(
            direct_message(
                room,
                reviewer,
                implementer,
                type=MessageType.REWORK_REQUEST,
            ),
            authenticated_sender_id=reviewer.member_id,
        )


def test_question_answer_preserves_reply_causality(tmp_path: Path) -> None:
    router, room, members, _ = make_context(tmp_path)
    planner = members[MemberRole.PLANNER]
    implementer = members[MemberRole.IMPLEMENTER]
    question = direct_message(
        room,
        implementer,
        planner,
        type=MessageType.QUESTION,
    )
    routed_question = router.route(
        question, authenticated_sender_id=implementer.member_id
    )
    answer = direct_message(
        room,
        planner,
        implementer,
        type=MessageType.ANSWER,
        reply_to=question.message_id,
        correlation_id=question.correlation_id,
        causation_id=question.message_id,
    )

    routed_answer = router.route(answer, authenticated_sender_id=planner.member_id)

    assert routed_answer.message.reply_to == routed_question.message.message_id
    invalid = direct_message(
        room,
        planner,
        implementer,
        type=MessageType.ANSWER,
    )
    with pytest.raises(RouteNotAllowedError, match="must reply to a question"):
        router.route(invalid, authenticated_sender_id=planner.member_id)


def test_artifacts_must_be_integrity_bound_to_room_task(tmp_path: Path) -> None:
    router, room, members, artifacts = make_context(tmp_path)
    implementer = members[MemberRole.IMPLEMENTER]
    reviewer = members[MemberRole.REVIEWER]
    reference = put_artifact(artifacts, room)
    shared = direct_message(
        room,
        implementer,
        reviewer,
        type=MessageType.ARTIFACT_SHARED,
        artifacts=(reference,),
    )
    assert router.route(
        shared, authenticated_sender_id=implementer.member_id
    ).message.artifacts == (reference,)

    foreign = put_artifact(artifacts, room, task_id=uuid4())
    with pytest.raises(ConversationArtifactError, match="another task"):
        router.route(
            direct_message(
                room,
                implementer,
                reviewer,
                type=MessageType.ARTIFACT_SHARED,
                artifacts=(foreign,),
            ),
            authenticated_sender_id=implementer.member_id,
        )
    tampered = reference.model_copy(update={"sha256": "0" * 64})
    with pytest.raises(ConversationArtifactError, match="does not match"):
        router.route(
            direct_message(
                room,
                implementer,
                reviewer,
                type=MessageType.ARTIFACT_SHARED,
                artifacts=(tampered,),
            ),
            authenticated_sender_id=implementer.member_id,
        )


def test_rework_requires_rejected_report_with_unresolved_issues(tmp_path: Path) -> None:
    router, room, members, artifacts = make_context(tmp_path)
    reviewer = members[MemberRole.REVIEWER]
    implementer = members[MemberRole.IMPLEMENTER]

    def review_reference(content: dict) -> ArtifactReference:
        metadata = artifacts.put_json(
            content,
            task_id=room.task_id,
            trace_id=room.trace_id,
            type=ArtifactType.REVIEW_REPORT,
            created_by="reviewer",
            filename=f"review-{uuid4()}.json",
        )
        return ArtifactReference.from_metadata(metadata, summary="review")

    base = {
        "task_id": str(room.task_id),
        "trace_id": str(room.trace_id),
        "reviewer": "reviewer",
        "verdict": "rejected",
        "issues": [
            {
                "issue_id": str(uuid4()),
                "priority": "high",
                "summary": "regression remains",
                "resolved": False,
            }
        ],
        "summary": "rework required",
    }
    valid = direct_message(
        room,
        reviewer,
        implementer,
        type=MessageType.REWORK_REQUEST,
        artifacts=(review_reference(base),),
    )
    assert router.route(valid, authenticated_sender_id=reviewer.member_id).message == valid

    invalid = direct_message(
        room,
        reviewer,
        implementer,
        type=MessageType.REWORK_REQUEST,
        artifacts=(review_reference({**base, "issues": []}),),
    )
    with pytest.raises(ConversationArtifactError, match="unresolved issue"):
        router.route(invalid, authenticated_sender_id=reviewer.member_id)

    forgotten = direct_message(
        room,
        reviewer,
        members[MemberRole.ORCHESTRATOR],
        type=MessageType.REVIEW_APPROVED,
        artifacts=(
            review_reference({**base, "verdict": "approved", "issues": []}),
        ),
    )
    with pytest.raises(ConversationArtifactError, match="carry forward"):
        router.route(forgotten, authenticated_sender_id=reviewer.member_id)
