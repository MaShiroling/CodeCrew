from uuid import UUID

from app.storage import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
    ArtifactType,
)
from app.team.models import (
    ChatMessage,
    MemberKind,
    MemberRole,
    MessageType,
    RecipientKind,
    RoomMember,
    StoredChatMessage,
)
from app.team.store import TeamRoomStore


class ConversationRoutingError(RuntimeError):
    """Raised when a chat message crosses an identity or workflow boundary."""


class SenderAuthenticationError(ConversationRoutingError):
    pass


class RouteNotAllowedError(ConversationRoutingError):
    pass


class ConversationArtifactError(ConversationRoutingError):
    pass


_ALLOWED_ROUTES: dict[MemberRole, frozenset[MemberRole]] = {
    MemberRole.PLANNER: frozenset(
        {MemberRole.IMPLEMENTER, MemberRole.ORCHESTRATOR, MemberRole.HUMAN}
    ),
    MemberRole.IMPLEMENTER: frozenset(
        {
            MemberRole.PLANNER,
            MemberRole.REVIEWER,
            MemberRole.ORCHESTRATOR,
            MemberRole.HUMAN,
        }
    ),
    MemberRole.REVIEWER: frozenset(
        {MemberRole.IMPLEMENTER, MemberRole.ORCHESTRATOR, MemberRole.HUMAN}
    ),
    MemberRole.VERIFIER: frozenset(
        {
            MemberRole.IMPLEMENTER,
            MemberRole.REVIEWER,
            MemberRole.ORCHESTRATOR,
            MemberRole.HUMAN,
        }
    ),
    MemberRole.ORCHESTRATOR: frozenset(MemberRole),
    MemberRole.HUMAN: frozenset(MemberRole),
}


class ConversationRouter:
    """Authenticate, authorize, validate, resolve, and persist one room message."""

    def __init__(self, rooms: TeamRoomStore, artifacts: ArtifactStore) -> None:
        if rooms.database.path != artifacts.database.path:
            raise ValueError("conversation router stores must share one database")
        self.rooms = rooms
        self.artifacts = artifacts

    def route(
        self,
        message: ChatMessage,
        *,
        authenticated_sender_id: UUID,
    ) -> StoredChatMessage:
        if authenticated_sender_id != message.sender_id:
            raise SenderAuthenticationError("authenticated member does not match sender_id")
        room = self.rooms.get_room(message.room_id)
        if room.task_id != message.task_id or room.trace_id != message.trace_id:
            raise ConversationRoutingError("message task or trace does not match the room")
        members = {member.member_id: member for member in room.members}
        try:
            sender = members[message.sender_id]
        except KeyError as exc:
            raise SenderAuthenticationError("sender is not a member of this room") from exc

        recipients = self._resolve_recipients(message, room.members)
        self._validate_routes(sender, recipients)
        self._validate_message_type(message, sender, recipients)
        self._validate_reply(message, recipients)
        self._validate_artifacts(message)
        return self.rooms.append_message(
            message,
            recipient_ids=tuple(member.member_id for member in recipients),
        )

    @staticmethod
    def _resolve_recipients(
        message: ChatMessage, members: tuple[RoomMember, ...]
    ) -> tuple[RoomMember, ...]:
        by_id = {member.member_id: member for member in members}
        resolved: dict[UUID, RoomMember] = {}
        for target in message.recipients:
            if target.kind is RecipientKind.MEMBER:
                member = by_id.get(target.member_id)
                if member is None:
                    raise RouteNotAllowedError("direct recipient is not a room member")
                resolved[member.member_id] = member
            elif target.kind is RecipientKind.ROLE:
                for member in members:
                    if member.role is target.role:
                        resolved[member.member_id] = member
            else:
                for member in members:
                    resolved[member.member_id] = member
        resolved.pop(message.sender_id, None)
        if not resolved:
            raise RouteNotAllowedError("message did not resolve to another room member")
        return tuple(resolved.values())

    @staticmethod
    def _validate_routes(sender: RoomMember, recipients: tuple[RoomMember, ...]) -> None:
        allowed = _ALLOWED_ROUTES[sender.role]
        denied = sorted(
            {recipient.role.value for recipient in recipients if recipient.role not in allowed}
        )
        if denied:
            raise RouteNotAllowedError(
                f"{sender.role.value} cannot message roles: {', '.join(denied)}"
            )

    @staticmethod
    def _validate_message_type(
        message: ChatMessage,
        sender: RoomMember,
        recipients: tuple[RoomMember, ...],
    ) -> None:
        if message.type is MessageType.SYSTEM_EVENT and sender.kind is not MemberKind.SYSTEM:
            raise RouteNotAllowedError("only a system identity may send system events")
        if message.type is MessageType.ISSUE_POSTED and sender.role not in {
            MemberRole.HUMAN,
            MemberRole.ORCHESTRATOR,
        }:
            raise RouteNotAllowedError("only human or orchestrator may post an issue")
        if message.type is MessageType.REVIEW_COMMENT and sender.role is not MemberRole.REVIEWER:
            raise RouteNotAllowedError("only a reviewer may send review comments")
        if message.type is MessageType.REWORK_REQUEST and sender.role not in {
            MemberRole.REVIEWER,
            MemberRole.ORCHESTRATOR,
        }:
            raise RouteNotAllowedError("only reviewer or orchestrator may request rework")
        if message.type is MessageType.REVIEW_APPROVED and sender.role is not MemberRole.REVIEWER:
            raise RouteNotAllowedError("only a reviewer may approve a review")
        if message.type is MessageType.VERIFICATION_READY and sender.role is not MemberRole.VERIFIER:
            raise RouteNotAllowedError("only verifier may publish verification evidence")
        if message.type in {
            MessageType.COMPLETION_PASSED,
            MessageType.COMPLETION_REJECTED,
        } and sender.role is not MemberRole.ORCHESTRATOR:
            raise RouteNotAllowedError("only orchestrator may publish completion decisions")
        if message.type is MessageType.HUMAN_INPUT_REQUEST and not any(
            recipient.role is MemberRole.HUMAN for recipient in recipients
        ):
            raise RouteNotAllowedError("human input requests must target a human member")
        if message.type is MessageType.ARTIFACT_SHARED and not message.artifacts:
            raise RouteNotAllowedError("artifact_shared messages require an artifact reference")
        if message.type is MessageType.PLAN_SHARED:
            if sender.role is not MemberRole.PLANNER:
                raise RouteNotAllowedError("only planner may publish a plan")
            if sum(
                reference.type is ArtifactType.PLAN for reference in message.artifacts
            ) != 1:
                raise RouteNotAllowedError("plan_shared requires exactly one plan artifact")
        if (
            message.type is MessageType.IMPLEMENTATION_READY
            and sender.role is not MemberRole.IMPLEMENTER
        ):
            raise RouteNotAllowedError(
                "only implementer may declare implementation ready"
            )
        required_artifact_types = {
            MessageType.VERIFICATION_READY: ArtifactType.VERIFICATION_REPORT,
            MessageType.REVIEW_APPROVED: ArtifactType.REVIEW_REPORT,
            MessageType.COMPLETION_PASSED: ArtifactType.COMPLETION_DECISION,
            MessageType.COMPLETION_REJECTED: ArtifactType.COMPLETION_DECISION,
        }
        required_type = required_artifact_types.get(message.type)
        if required_type is not None and not any(
            reference.type is required_type for reference in message.artifacts
        ):
            raise RouteNotAllowedError(
                f"{message.type.value} requires a {required_type.value} artifact"
            )

    def _validate_reply(
        self,
        message: ChatMessage,
        recipients: tuple[RoomMember, ...],
    ) -> None:
        if message.reply_to is None:
            if message.type is MessageType.ANSWER:
                raise RouteNotAllowedError("an answer must reply to a question")
            return
        parent = self.rooms.get_message(message.reply_to).message
        if parent.room_id != message.room_id:
            raise RouteNotAllowedError("reply target belongs to another room")
        if message.correlation_id != parent.correlation_id:
            raise RouteNotAllowedError("a reply must preserve its correlation_id")
        if message.causation_id != parent.message_id:
            raise RouteNotAllowedError("a reply must identify its parent as causation_id")
        if message.type is MessageType.ANSWER and parent.type is not MessageType.QUESTION:
            raise RouteNotAllowedError("an answer must reply to a question")
        if parent.sender_id not in {recipient.member_id for recipient in recipients}:
            raise RouteNotAllowedError("a reply must include the original sender")

    def _validate_artifacts(self, message: ChatMessage) -> None:
        for reference in message.artifacts:
            try:
                metadata = self.artifacts.get_metadata(reference.artifact_id)
                if metadata.task_id != message.task_id or metadata.trace_id != message.trace_id:
                    raise ConversationArtifactError(
                        "artifact belongs to another task or trace"
                    )
                if metadata.type is not reference.type or metadata.sha256 != reference.sha256:
                    raise ConversationArtifactError(
                        "artifact reference does not match stored metadata"
                    )
                for _ in self.artifacts.iter_bytes(reference.artifact_id):
                    pass
            except (ArtifactNotFoundError, ArtifactIntegrityError) as exc:
                raise ConversationArtifactError(str(exc)) from exc
