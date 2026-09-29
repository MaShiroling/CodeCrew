"""Human @mention contract for queued, non-executing team discussion."""

import re
from uuid import uuid5

from app.api.models import PostDiscussionMessageRequest
from app.api.service import TaskDetailUnavailable, TaskMessageInvalid, TaskMessageNotFound
from app.orchestration.models import Task
from app.team.models import (
    ChatMessage,
    MemberKind,
    MemberRole,
    MessageRecipient,
    MessageType,
    RecipientKind,
    TeamRoom,
)
from app.team.personas import default_team_personas
from app.team.store import ChatMessageNotFoundError, TeamRoomStore

_MENTION = re.compile(r"(?<![\w@])@[\w]+", re.UNICODE)
_AGENT_ROLES = frozenset({MemberRole.PLANNER, MemberRole.IMPLEMENTER, MemberRole.REVIEWER})


def build_discussion_message(
    task: Task, room: TeamRoom, rooms: TeamRoomStore, request: PostDiscussionMessageRequest,
) -> tuple[ChatMessage, tuple[MemberRole, ...]]:
    """Resolve exact persona aliases; a reply without mentions addresses its Agent author."""
    humans = tuple(member for member in room.members
                   if member.role is MemberRole.HUMAN and member.kind is MemberKind.HUMAN)
    if len(humans) != 1:
        raise TaskDetailUnavailable("task room must have exactly one human identity")
    human = humans[0]

    aliases = {
        alias.casefold(): profile.role
        for profile in default_team_personas().profiles
        for alias in profile.mention_patterns
    }
    tokens = _MENTION.findall(request.content)
    unknown = [token for token in tokens if token.casefold() not in aliases]
    if unknown:
        raise TaskMessageInvalid("discussion contains an unknown Agent mention")
    if not _MENTION.sub("", request.content).strip(" \t\r\n,，。.!?？;；:："):
        raise TaskMessageInvalid("discussion needs content beyond Agent mentions")
    roles = tuple(dict.fromkeys(aliases[token.casefold()] for token in tokens))

    parent = None
    if request.reply_to is not None:
        try:
            parent = rooms.get_message(request.reply_to).message
        except ChatMessageNotFoundError as exc:
            raise TaskMessageNotFound("discussion reply target is not in this task room") from exc
        if (parent.room_id != room.room_id or parent.task_id != task.id
                or parent.trace_id != task.trace_id):
            raise TaskMessageNotFound("discussion reply target is not in this task room")
        author = next((member for member in room.members
                       if member.member_id == parent.sender_id), None)
        if author is None or author.role not in _AGENT_ROLES or author.kind is not MemberKind.AGENT:
            raise TaskMessageInvalid("discussion replies require an Agent author")
        # A reply must include its original sender even when it also @mentions teammates.
        roles = tuple(dict.fromkeys((author.role, *roles)))
    if not roles:
        raise TaskMessageInvalid("discussion requires an Agent @mention or an Agent reply target")

    recipients = []
    for role in roles:
        matches = [member for member in room.members
                   if member.role is role and member.kind is MemberKind.AGENT]
        if len(matches) != 1:
            raise TaskDetailUnavailable("discussion target Agent identity is unavailable")
        recipients.append(MessageRecipient(kind=RecipientKind.MEMBER, member_id=matches[0].member_id))

    return ChatMessage(
        room_id=room.room_id, task_id=task.id, trace_id=task.trace_id,
        sender_id=human.member_id, recipients=tuple(recipients),
        type=MessageType.DISCUSSION, content=request.content,
        reply_to=parent.message_id if parent else None,
        correlation_id=(parent.correlation_id if parent else
                        uuid5(task.id, f"human-discussion:{request.idempotency_key}")),
        causation_id=parent.message_id if parent else None,
        idempotency_key=f"human-discussion:{request.idempotency_key}",
    ), roles
