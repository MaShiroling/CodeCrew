"""Local human intent validation; no scheduling, permission changes or implicit ACK."""

from uuid import uuid5

from app.api.details import RoomMessageView
from app.api.models import PostHumanMessageRequest
from app.api.service import TaskDetailUnavailable, TaskMessageInvalid, TaskMessageNotFound
from app.orchestration.models import Task
from app.team.models import (
    ChatMessage,
    MemberKind,
    MemberRole,
    MessageDeliveryStatus,
    MessageRecipient,
    MessageType,
    RecipientKind,
    StoredChatMessage,
    TeamRoom,
)
from app.team.store import ChatMessageNotFoundError, TeamRoomStore


def build_human_message(
    task: Task, room: TeamRoom, rooms: TeamRoomStore, request: PostHumanMessageRequest,
) -> ChatMessage:
    humans = [member for member in room.members
              if member.role is MemberRole.HUMAN and member.kind is MemberKind.HUMAN]
    if len(humans) != 1:
        raise TaskDetailUnavailable("task room must have exactly one human identity")
    human = humans[0]
    parent = None
    if request.reply_to is not None:
        try:
            stored = rooms.get_message(request.reply_to)
        except ChatMessageNotFoundError as exc:
            raise TaskMessageNotFound("reply target is not in this task room") from exc
        parent = stored.message
        if parent.room_id != room.room_id or parent.task_id != task.id or parent.trace_id != task.trace_id:
            raise TaskMessageNotFound("reply target is not in this task room")
        if parent.type not in {MessageType.QUESTION, MessageType.HUMAN_INPUT_REQUEST}:
            raise TaskMessageInvalid("only questions or human input requests may be answered")
        if not any(delivery.recipient_id == human.member_id
                   and delivery.status is MessageDeliveryStatus.PENDING for delivery in stored.deliveries):
            raise TaskMessageInvalid("reply target is not pending for this human")
        recipient_ids = [parent.sender_id]
    else:
        recipient_ids = [member.member_id for member in room.members
                         if member.role is request.recipient_role]
    if len(recipient_ids) != 1:
        raise TaskDetailUnavailable("human message requires exactly one recipient identity")
    recipient = next((member for member in room.members if member.member_id == recipient_ids[0]), None)
    if recipient is None or recipient.role not in {
        MemberRole.PLANNER, MemberRole.IMPLEMENTER, MemberRole.REVIEWER, MemberRole.ORCHESTRATOR,
    }:
        raise TaskMessageInvalid("human messages cannot target this identity")
    return ChatMessage(
        room_id=room.room_id, task_id=task.id, trace_id=task.trace_id,
        sender_id=human.member_id,
        recipients=(MessageRecipient(kind=RecipientKind.MEMBER, member_id=recipient_ids[0]),),
        type=MessageType.ANSWER if parent and parent.type is MessageType.QUESTION else MessageType.MESSAGE,
        content=request.content,
        reply_to=parent.message_id if parent else None,
        correlation_id=parent.correlation_id if parent else uuid5(task.id, f"human-api:{request.idempotency_key}"),
        causation_id=parent.message_id if parent else None,
        idempotency_key=f"human-api:{request.idempotency_key}",
    )


def message_view(stored: StoredChatMessage, room: TeamRoom) -> RoomMessageView:
    message = stored.message
    sender = next((member for member in room.members if member.member_id == message.sender_id), None)
    if sender is None:
        raise TaskDetailUnavailable("message sender is not in the task room")
    return RoomMessageView(
        sequence=stored.sequence, message_id=message.message_id,
        sender_id=message.sender_id, sender_name=sender.name, sender_role=sender.role,
        recipient_ids=tuple(delivery.recipient_id for delivery in stored.deliveries),
        type=message.type, content=message.content, artifacts=message.artifacts,
        reply_to=message.reply_to, correlation_id=message.correlation_id, created_at=message.created_at,
    )
