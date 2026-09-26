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
from app.trace import TraceActorKind, TraceEvent, TraceEventType, TraceStore
from app.verification import ReviewIssue, ReviewIssuePriority, ReviewVerdict


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

    def __init__(
        self,
        rooms: TeamRoomStore,
        artifacts: ArtifactStore,
        trace_store: TraceStore | None = None,
    ) -> None:
        if rooms.database.path != artifacts.database.path:
            raise ValueError("conversation router stores must share one database")
        self.rooms = rooms
        self.artifacts = artifacts
        self.trace_store = trace_store or TraceStore(rooms.database)
        self.trace_store.initialize()

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
        if message.type in {MessageType.REWORK_REQUEST, MessageType.REVIEW_APPROVED}:
            self._validate_review_report(message)
        stored = self.rooms.append_message(
            message,
            recipient_ids=tuple(member.member_id for member in recipients),
        )
        actor_kind = {
            MemberKind.AGENT: TraceActorKind.AGENT,
            MemberKind.SYSTEM: TraceActorKind.SYSTEM,
            MemberKind.HUMAN: TraceActorKind.HUMAN,
        }[sender.kind]
        persisted_message = stored.message
        self.trace_store.append(
            TraceEvent(
                task_id=persisted_message.task_id,
                trace_id=persisted_message.trace_id,
                type=TraceEventType.CHAT_MESSAGE_PERSISTED,
                actor_kind=actor_kind,
                actor_id=str(sender.member_id),
                correlation_id=persisted_message.correlation_id,
                causation_id=persisted_message.causation_id,
                idempotency_key=f"chat-message:{persisted_message.message_id}",
                occurred_at=persisted_message.created_at,
                payload={
                    "message_id": str(persisted_message.message_id),
                    "sequence": stored.sequence,
                    "message_type": persisted_message.type.value,
                    "sender_role": sender.role.value,
                    "recipient_ids": [
                        str(delivery.recipient_id) for delivery in stored.deliveries
                    ],
                    "artifact_ids": [
                        str(reference.artifact_id)
                        for reference in persisted_message.artifacts
                    ],
                },
            )
        )
        semantic_type = {
            MessageType.REVIEW_APPROVED: TraceEventType.REVIEW_DECIDED,
            MessageType.REWORK_REQUEST: TraceEventType.REVIEW_DECIDED,
            MessageType.HUMAN_INPUT_REQUEST: TraceEventType.HUMAN_INPUT_REQUESTED,
        }.get(persisted_message.type)
        if semantic_type is not None:
            self.trace_store.append(
                TraceEvent(
                    task_id=persisted_message.task_id,
                    trace_id=persisted_message.trace_id,
                    type=semantic_type,
                    actor_kind=actor_kind,
                    actor_id=str(sender.member_id),
                    correlation_id=persisted_message.correlation_id,
                    causation_id=persisted_message.message_id,
                    idempotency_key=f"semantic-message:{persisted_message.message_id}",
                    occurred_at=persisted_message.created_at,
                    payload={
                        "message_id": str(persisted_message.message_id),
                        "message_type": persisted_message.type.value,
                        "artifact_ids": [
                            str(item.artifact_id)
                            for item in persisted_message.artifacts
                        ],
                    },
                )
            )
        return stored

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
            MessageType.REWORK_REQUEST: ArtifactType.REVIEW_REPORT,
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

    def _validate_review_report(self, message: ChatMessage) -> None:
        reference = next(
            item for item in message.artifacts if item.type is ArtifactType.REVIEW_REPORT
        )
        try:
            content = self.artifacts.read_json(reference.artifact_id)
            if not isinstance(content, dict):
                raise TypeError("review report must be a JSON object")
            if content.get("task_id") != str(message.task_id):
                raise ValueError("review report task_id does not match the message")
            if content.get("trace_id") != str(message.trace_id):
                raise ValueError("review report trace_id does not match the message")
            expected_verdict = (
                ReviewVerdict.REJECTED
                if message.type is MessageType.REWORK_REQUEST
                else ReviewVerdict.APPROVED
            )
            if ReviewVerdict(content.get("verdict")) is not expected_verdict:
                raise ValueError(
                    f"{message.type.value} review verdict must be {expected_verdict.value}"
                )
            raw_issues = content.get("issues")
            if not isinstance(raw_issues, list):
                raise TypeError("rework review issues must be a list")
            if any(
                not isinstance(item, dict) or "issue_id" not in item
                for item in raw_issues
            ):
                raise ValueError("every review issue requires a stable issue_id")
            issues = tuple(ReviewIssue.model_validate(item) for item in raw_issues)
            if len({issue.issue_id for issue in issues}) != len(issues):
                raise ValueError("review issues must have unique issue IDs")
            if message.type is MessageType.REWORK_REQUEST and (
                not issues or all(issue.resolved for issue in issues)
            ):
                raise ValueError("rework review requires an unresolved issue")
            current_by_id = {issue.issue_id: issue for issue in issues}
            missing = tuple(
                issue.issue_id
                for issue in self._unresolved_review_issues(message.room_id)
                if issue.issue_id not in current_by_id
            )
            if missing:
                raise ValueError("review report must carry forward unresolved issue IDs")
            if expected_verdict is ReviewVerdict.APPROVED and any(
                not issue.resolved
                and issue.priority
                in {ReviewIssuePriority.HIGH, ReviewIssuePriority.CRITICAL}
                for issue in issues
            ):
                raise ValueError(
                    "approved review cannot contain unresolved high-priority issues"
                )
        except (TypeError, ValueError) as exc:
            raise ConversationArtifactError(f"invalid review report: {exc}") from exc

    def _unresolved_review_issues(self, room_id: UUID) -> tuple[ReviewIssue, ...]:
        latest: dict[UUID, ReviewIssue] = {}
        for stored in self.rooms.list_messages(room_id, limit=1_000):
            if stored.message.type not in {
                MessageType.REWORK_REQUEST,
                MessageType.REVIEW_APPROVED,
            }:
                continue
            reference = next(
                (
                    item
                    for item in stored.message.artifacts
                    if item.type is ArtifactType.REVIEW_REPORT
                ),
                None,
            )
            if reference is None:
                continue
            content = self.artifacts.read_json(reference.artifact_id)
            if not isinstance(content, dict) or not isinstance(content.get("issues"), list):
                continue
            for item in content["issues"]:
                issue = ReviewIssue.model_validate(item)
                latest[issue.issue_id] = issue
        return tuple(issue for issue in latest.values() if not issue.resolved)
