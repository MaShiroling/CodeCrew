"""Local HTTP-facing operations for repository-free chat; no Agent execution."""

import re
from uuid import UUID, uuid5

from app.chat.models import (
    StandaloneChatMessage,
    StandaloneChatRoom,
    StoredStandaloneChatMessage,
)
from app.chat.store import (
    StandaloneChatConflictError,
    StandaloneChatIdempotencyError,
    StandaloneChatMessageNotFoundError,
    StandaloneChatRoomNotFoundError,
    StandaloneChatStore,
)
from app.team.models import MemberKind, MemberRole, RoomMember
from app.team.personas import default_team_personas

_MENTION = re.compile(r"(?<![\w@])@[\w]+", re.UNICODE)
_AGENT_ROLES = frozenset({MemberRole.PLANNER, MemberRole.IMPLEMENTER, MemberRole.REVIEWER})


class ChatApiError(RuntimeError):
    code = "chat_error"
    status_code = 400


class ChatNotFound(ChatApiError):
    code = "chat_not_found"
    status_code = 404


class ChatMessageNotFound(ChatApiError):
    code = "chat_message_not_found"
    status_code = 404


class ChatConflict(ChatApiError):
    code = "chat_conflict"
    status_code = 409


class ChatInvalid(ChatApiError):
    code = "chat_message_invalid"
    status_code = 422


class ChatServiceUnavailable(ChatApiError):
    code = "chat_service_unavailable"
    status_code = 503


class StandaloneChatService:
    """Only persists local human messages; dispatch and code authority are absent."""

    def __init__(self, store: StandaloneChatStore) -> None:
        self.store = store

    def create_room(self, *, title: str, idempotency_key: UUID) -> StandaloneChatRoom:
        # Client-provided key yields a stable room identity across HTTP retries.
        room_id = uuid5(idempotency_key, "codecrew:standalone-chat-room")
        try:
            existing = self.store.get_room(room_id)
        except StandaloneChatRoomNotFoundError:
            existing = None
        if existing is not None:
            if existing.title != title:
                raise ChatConflict("room creation key was used for another title")
            return existing
        catalog = default_team_personas()
        members = (RoomMember(
            member_id=uuid5(room_id, "human"), room_id=room_id,
            name="human", role=MemberRole.HUMAN, kind=MemberKind.HUMAN,
        ), *(
            RoomMember(
                member_id=uuid5(room_id, profile.role.value), room_id=room_id,
                name=profile.display_name, role=profile.role, kind=MemberKind.AGENT,
            ) for profile in catalog.profiles
        ))
        room = StandaloneChatRoom(
            room_id=room_id, trace_id=uuid5(room_id, "trace"),
            title=title, members=members,
        )
        try:
            return self.store.create_room(room)
        except StandaloneChatConflictError as exc:
            # A concurrent identical creation may have won the race.
            existing = self.store.get_room(room_id)
            if existing.title == title:
                return existing
            raise ChatConflict("room creation key was used for another title") from exc

    def get_room(self, room_id: UUID) -> StandaloneChatRoom:
        try:
            return self.store.get_room(room_id)
        except StandaloneChatRoomNotFoundError as exc:
            raise ChatNotFound("chat room not found") from exc

    def list_rooms(self, *, limit: int, offset: int) -> tuple[StandaloneChatRoom, ...]:
        return self.store.list_rooms(limit=limit, offset=offset)

    def list_messages(
        self, room_id: UUID, *, after_sequence: int, limit: int,
    ) -> tuple[StoredStandaloneChatMessage, ...]:
        try:
            return self.store.list_messages(
                room_id, after_sequence=after_sequence, limit=limit,
            )
        except StandaloneChatRoomNotFoundError as exc:
            raise ChatNotFound("chat room not found") from exc

    def post_message(
        self, room_id: UUID, *, content: str, idempotency_key: UUID,
        reply_to: UUID | None, context_anchor_id: UUID | None = None,
    ) -> StoredStandaloneChatMessage:
        room = self.get_room(room_id)
        if reply_to is not None and context_anchor_id is not None:
            raise ChatInvalid("choose a reply or an explicit context anchor, not both")
        human = next(member for member in room.members if member.role is MemberRole.HUMAN)
        aliases = {
            alias.casefold(): profile.role
            for profile in default_team_personas().profiles
            for alias in profile.mention_patterns
        }
        tokens = _MENTION.findall(content)
        if any(token.casefold() not in aliases for token in tokens):
            raise ChatInvalid("chat contains an unknown Agent mention")
        if not _MENTION.sub("", content).strip(" \t\r\n,，。.!?？;；:："):
            raise ChatInvalid("chat needs content beyond Agent mentions")
        roles = tuple(dict.fromkeys(aliases[token.casefold()] for token in tokens))
        parent = None
        if reply_to is not None:
            try:
                parent = self.store.get_message(reply_to).message
            except StandaloneChatMessageNotFoundError as exc:
                raise ChatMessageNotFound("reply target is not in this chat room") from exc
            if parent.room_id != room_id:
                raise ChatMessageNotFound("reply target is not in this chat room")
            author = next(
                (member for member in room.members if member.member_id == parent.sender_id),
                None,
            )
            if author is None or author.role not in _AGENT_ROLES:
                raise ChatInvalid("chat replies require an Agent author")
            roles = tuple(dict.fromkeys((author.role, *roles)))
        if not roles:
            raise ChatInvalid("chat requires an Agent @mention or an Agent reply target")
        if context_anchor_id is not None:
            try:
                anchor = self.store.get_message(context_anchor_id).message
            except StandaloneChatMessageNotFoundError as exc:
                raise ChatMessageNotFound("context anchor is not in this chat room") from exc
            if anchor.room_id != room_id:
                raise ChatMessageNotFound("context anchor is not in this chat room")
            if anchor.sender_id != human.member_id:
                raise ChatInvalid("context anchor must be a Human message")
        elif parent is not None:
            context_anchor_id = parent.context_anchor_id
        recipients = tuple(
            next(member.member_id for member in room.members if member.role is role)
            for role in roles
        )
        message = StandaloneChatMessage(
            room_id=room_id, trace_id=room.trace_id, sender_id=human.member_id,
            recipient_ids=recipients, content=content,
            reply_to=reply_to, context_anchor_id=context_anchor_id,
            correlation_id=(parent.correlation_id if parent else
                            uuid5(room_id, f"human-chat:{idempotency_key}")),
            causation_id=reply_to,
            idempotency_key=f"human-chat:{idempotency_key}",
        )
        try:
            return self.store.append_message(message)
        except StandaloneChatIdempotencyError as exc:
            raise ChatConflict("message key was used for different content") from exc
        except StandaloneChatConflictError as exc:
            raise ChatConflict(str(exc)) from exc
