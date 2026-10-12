"""Local HTTP-facing operations for repository-free chat; no Agent execution."""

import re
from typing import Literal
from uuid import UUID, uuid5

from app.chat.memory import ChatMemory, ChatMemoryCandidate
from app.chat.models import (
    ExternalChatSource,
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

    def create_memory(
        self, room_id: UUID, *, source_message_id: UUID, content: str,
        replaces_memory_id: UUID | None = None,
    ) -> ChatMemory:
        """Only a local Human message can be promoted to durable room memory."""
        room = self.get_room(room_id)
        try:
            source = self.store.get_message(source_message_id).message
        except StandaloneChatMessageNotFoundError as exc:
            raise ChatMessageNotFound("memory source not found in this room") from exc
        human = next(member for member in room.members if member.role is MemberRole.HUMAN)
        if source.room_id != room_id:
            raise ChatMessageNotFound("memory source not found in this room")
        if (source.sender_id != human.member_id or source.external_source is not None
                or self.store.is_external_discussion(source.correlation_id)):
            raise ChatInvalid("memory source must be a local Human message")
        try:
            return self.store.memory.create(
                room_id, source_message_id, content,
                replaces_memory_id=replaces_memory_id,
            )
        except ValueError as exc:
            raise ChatConflict(str(exc)) from exc

    def list_memories(
        self, room_id: UUID, *, include_closed: bool = False,
    ) -> tuple[ChatMemory, ...]:
        self.get_room(room_id)
        active = self.store.memory.list_active(room_id)
        return active + self.store.memory.list_closed_open_loops(room_id) if include_closed else active

    def close_open_loop(
        self, room_id: UUID, memory_id: UUID, *, action: Literal["complete", "cancel"],
    ) -> ChatMemory:
        self.get_room(room_id)
        try:
            memory = self.store.memory.close_open_loop(room_id, memory_id, action=action)
        except ValueError as exc:
            raise ChatConflict(str(exc)) from exc
        if memory is None:
            raise ChatNotFound("chat memory not found")
        return memory

    def memory_lineage(
        self, room_id: UUID, memory_id: UUID,
    ) -> tuple[tuple[ChatMemory, ...], bool]:
        self.get_room(room_id)
        result = self.store.memory.lineage(room_id, memory_id)
        if result is None:
            raise ChatNotFound("chat memory not found")
        return result

    def list_memory_candidates(self, room_id: UUID) -> tuple[ChatMemoryCandidate, ...]:
        self.get_room(room_id)
        return self.store.memory.list_pending(room_id)

    def approve_memory_candidate(
        self, room_id: UUID, candidate_id: UUID, content: str,
        replaces_memory_id: UUID | None = None,
    ) -> tuple[ChatMemoryCandidate, ChatMemory]:
        self.get_room(room_id)
        try:
            approved = self.store.memory.approve_candidate(
                room_id, candidate_id, content,
                replaces_memory_id=replaces_memory_id,
            )
        except ValueError as exc:
            raise ChatConflict(str(exc)) from exc
        if approved is None:
            raise ChatNotFound("memory candidate not found")
        return approved

    def reject_memory_candidate(
        self, room_id: UUID, candidate_id: UUID,
    ) -> ChatMemoryCandidate:
        self.get_room(room_id)
        try:
            candidate = self.store.memory.reject_candidate(room_id, candidate_id)
        except ValueError as exc:
            raise ChatConflict(str(exc)) from exc
        if candidate is None:
            raise ChatNotFound("memory candidate not found")
        return candidate

    def retire_memory(self, room_id: UUID, memory_id: UUID) -> ChatMemory:
        self.get_room(room_id)
        memory = self.store.memory.retire(room_id, memory_id)
        if memory is None:
            raise ChatNotFound("chat memory not found")
        return memory

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
            if self.store.is_external_discussion(parent.correlation_id):
                raise ChatInvalid("start a new local discussion instead of replying into Feishu")
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

    @staticmethod
    def external_opening_role(
        content: str, opening_role: MemberRole | None = None,
    ) -> MemberRole:
        aliases = {
            alias.casefold(): profile.role
            for profile in default_team_personas().profiles
            for alias in profile.mention_patterns
        }
        tokens = _MENTION.findall(content)
        if any(token.casefold() not in aliases for token in tokens):
            raise ChatInvalid("external chat contains an unknown Agent mention")
        if not _MENTION.sub("", content).strip(" \t\r\n,，。.!?？;；:："):
            raise ChatInvalid("chat needs content beyond Agent mentions")
        selected = aliases[tokens[0].casefold()] if tokens else opening_role or MemberRole.PLANNER
        if selected not in _AGENT_ROLES or (tokens and opening_role and selected != opening_role):
            raise ChatInvalid("external opening role conflicts with Agent mention")
        return selected

    def post_external_message(
        self, room_id: UUID, *, content: str, idempotency_key: UUID,
        external_source: ExternalChatSource, opening_role: MemberRole | None = None,
    ) -> StoredStandaloneChatMessage:
        """Restricted bridge entry: one fresh read-only root, no dispatcher or Task."""
        source = ExternalChatSource.model_validate(external_source.model_dump())
        role = self.external_opening_role(content, opening_role)
        room = self.get_room(room_id)
        human = next(member for member in room.members if member.role is MemberRole.HUMAN)
        recipient = next(member for member in room.members if member.role is role)
        key = f"external-chat:{idempotency_key}"
        message = StandaloneChatMessage(
            message_id=uuid5(room_id, key), room_id=room_id, trace_id=room.trace_id,
            sender_id=human.member_id, recipient_ids=(recipient.member_id,),
            content=content, external_source=source, correlation_id=uuid5(room_id, key + ':thread'),
            idempotency_key=key,
        )
        try:
            return self.store.append_message(message)
        except StandaloneChatIdempotencyError as exc:
            raise ChatConflict("external message key was used for different content") from exc
