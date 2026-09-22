import hashlib
import json
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.agents import (
    AgentCapability,
    AgentEvent,
    AgentExitReason,
    AgentRegistry,
    AgentRequest,
    AgentResult,
    AgentRole,
    AgentSession,
    PermissionMode,
)
from app.orchestration.models import Task
from app.storage import ArtifactReference, ArtifactStore
from app.team.actions import (
    AgentChatAction,
    AgentChatTurn,
    ChatActionType,
    parse_agent_chat_turn,
)
from app.team.models import (
    ChatMessage,
    MemberKind,
    MemberRole,
    MessageType,
    StoredChatMessage,
)
from app.team.router import ConversationRouter
from app.team.store import TeamRoomStore


class AgentTurnError(RuntimeError):
    """Raised when an Agent turn cannot be safely completed and acknowledged."""


class AgentTurnResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session: AgentSession
    agent_result: AgentResult
    events: tuple[AgentEvent, ...]
    consumed_message_ids: tuple[UUID, ...]
    routed_messages: tuple[StoredChatMessage, ...]
    finish_summary: str | None = None


_AGENT_ROLES = {
    MemberRole.PLANNER: AgentRole.PLANNER,
    MemberRole.IMPLEMENTER: AgentRole.IMPLEMENTER,
    MemberRole.REVIEWER: AgentRole.REVIEWER,
}

_PERMISSIONS = {
    MemberRole.PLANNER: PermissionMode.READ_ONLY,
    MemberRole.IMPLEMENTER: PermissionMode.WORKSPACE_WRITE,
    MemberRole.REVIEWER: PermissionMode.READ_ONLY,
}

_CAPABILITIES = {
    MemberRole.PLANNER: frozenset({AgentCapability.REPOSITORY_ANALYSIS}),
    MemberRole.IMPLEMENTER: frozenset({AgentCapability.CODE_EDIT}),
    MemberRole.REVIEWER: frozenset({AgentCapability.CODE_REVIEW}),
}

_MESSAGE_TYPES = {
    ChatActionType.SEND_MESSAGE: MessageType.MESSAGE,
    ChatActionType.ASK_QUESTION: MessageType.QUESTION,
    ChatActionType.ANSWER_QUESTION: MessageType.ANSWER,
    ChatActionType.SHARE_ARTIFACT: MessageType.ARTIFACT_SHARED,
    ChatActionType.SHARE_PLAN: MessageType.PLAN_SHARED,
    ChatActionType.REPORT_PROGRESS: MessageType.STATUS_UPDATE,
    ChatActionType.REQUEST_REVIEW: MessageType.IMPLEMENTATION_READY,
    ChatActionType.APPROVE_REVIEW: MessageType.REVIEW_APPROVED,
    ChatActionType.REQUEST_REWORK: MessageType.REWORK_REQUEST,
    ChatActionType.REQUEST_HUMAN_INPUT: MessageType.HUMAN_INPUT_REQUEST,
}


class AgentTurnRunner:
    """Execute one mailbox-driven Agent turn and persist its routed actions."""

    def __init__(
        self,
        registry: AgentRegistry,
        router: ConversationRouter,
        *,
        timeout_seconds: int = 900,
        pending_limit: int = 20,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if pending_limit <= 0:
            raise ValueError("pending_limit must be positive")
        self.registry = registry
        self.router = router
        self.rooms: TeamRoomStore = router.rooms
        self.artifacts: ArtifactStore = router.artifacts
        self.timeout_seconds = timeout_seconds
        self.pending_limit = pending_limit

    async def run(
        self,
        task: Task,
        *,
        room_id: UUID,
        member_id: UUID,
        agent_name: str,
        working_directory: Path,
        resume_native_session_id: str | None = None,
    ) -> AgentTurnResult:
        room = self.rooms.get_room(room_id)
        if room.task_id != task.id or room.trace_id != task.trace_id:
            raise AgentTurnError("task does not match the requested team room")
        member = self.rooms.get_member(member_id)
        if member.room_id != room_id:
            raise AgentTurnError("member does not belong to the requested team room")
        if member.kind is not MemberKind.AGENT or member.role not in _AGENT_ROLES:
            raise AgentTurnError("only planner, implementer, or reviewer agents can run turns")
        incoming = self.rooms.pending_for(member_id, limit=self.pending_limit)
        if not incoming:
            raise AgentTurnError("agent has no pending room messages")

        request = AgentRequest(
            task_id=task.id,
            trace_id=task.trace_id,
            role=_AGENT_ROLES[member.role],
            prompt=self._build_prompt(task, room.members, member_id, incoming),
            working_directory=working_directory,
            permission_mode=_PERMISSIONS[member.role],
            timeout_seconds=self.timeout_seconds,
            resume_from_session_id=resume_native_session_id,
            metadata={
                "room_id": str(room_id),
                "member_id": str(member_id),
                "input_message_ids": [
                    str(item.message.message_id) for item in incoming
                ],
            },
        )
        async with self.registry.acquire(
            agent_name,
            role=_AGENT_ROLES[member.role],
            permission_mode=_PERMISSIONS[member.role],
            required_capabilities=_CAPABILITIES[member.role],
        ) as adapter:
            if resume_native_session_id is None:
                session = await adapter.start(request)
            else:
                session = await adapter.resume(resume_native_session_id, request)
            events = tuple([event async for event in adapter.stream(session.session_id)])
            result = await adapter.wait(session.session_id)

        if result.trace_id != task.trace_id:
            raise AgentTurnError("agent result belongs to another trace")
        if result.reason is not AgentExitReason.COMPLETED or result.exit_code not in {0, None}:
            detail = result.error or result.reason.value
            raise AgentTurnError(f"agent turn failed: {detail}")
        turn = parse_agent_chat_turn(result.output)
        routed = self._route_actions(task, member_id, incoming, turn)
        for item in incoming:
            self.rooms.acknowledge(item.message.message_id, recipient_id=member_id)
        finish = turn.actions[-1].content
        return AgentTurnResult(
            session=session,
            agent_result=result,
            events=events,
            consumed_message_ids=tuple(item.message.message_id for item in incoming),
            routed_messages=routed,
            finish_summary=finish,
        )

    def _route_actions(
        self,
        task: Task,
        member_id: UUID,
        incoming: tuple[StoredChatMessage, ...],
        turn: AgentChatTurn,
    ) -> tuple[StoredChatMessage, ...]:
        latest = incoming[-1].message
        turn_key = self._turn_key(member_id, incoming)
        routed: list[StoredChatMessage] = []
        for index, action in enumerate(turn.actions):
            if action.action is ChatActionType.FINISH_TURN:
                continue
            parent = (
                self.rooms.get_message(action.reply_to).message
                if action.reply_to is not None
                else None
            )
            correlation_id = parent.correlation_id if parent else latest.correlation_id
            causation_id = parent.message_id if parent else latest.message_id
            artifacts = self._artifact_references(task, action)
            outgoing = ChatMessage(
                room_id=latest.room_id,
                task_id=task.id,
                trace_id=task.trace_id,
                sender_id=member_id,
                recipients=(action.recipient,),
                type=_MESSAGE_TYPES[action.action],
                content=action.content or "finished",
                artifacts=artifacts,
                reply_to=action.reply_to,
                correlation_id=correlation_id,
                causation_id=causation_id,
                idempotency_key=f"agent-turn:{turn_key}:{index}",
            )
            routed.append(
                self.router.route(outgoing, authenticated_sender_id=member_id)
            )
        return tuple(routed)

    def _artifact_references(
        self, task: Task, action: AgentChatAction
    ) -> tuple[ArtifactReference, ...]:
        references: list[ArtifactReference] = []
        for artifact_id in action.artifact_ids:
            metadata = self.artifacts.get_metadata(artifact_id)
            if metadata.task_id != task.id or metadata.trace_id != task.trace_id:
                raise AgentTurnError("agent action referenced an artifact from another task")
            references.append(
                ArtifactReference.from_metadata(
                    metadata,
                    summary=action.content or metadata.filename or "Agent-shared artifact",
                )
            )
        return tuple(references)

    def _build_prompt(
        self,
        task: Task,
        members: tuple,
        member_id: UUID,
        incoming: tuple[StoredChatMessage, ...],
    ) -> str:
        roster = [
            {
                "member_id": str(member.member_id),
                "name": member.name,
                "role": member.role.value,
            }
            for member in members
        ]
        messages = []
        for item in incoming:
            message = item.message
            messages.append(
                {
                    "message_id": str(message.message_id),
                    "sender_id": str(message.sender_id),
                    "type": message.type.value,
                    "content": message.content,
                    "correlation_id": str(message.correlation_id),
                    "artifacts": [
                        {
                            "artifact_id": str(reference.artifact_id),
                            "type": reference.type.value,
                            "path": str(
                                self.artifacts.blob_path_for(reference.artifact_id)
                            ),
                        }
                        for reference in message.artifacts
                    ],
                }
            )
        schema = {
            "actions": [
                {
                    "action": "send_message | ask_question | answer_question | "
                    "share_artifact | share_plan | report_progress | request_review | "
                    "approve_review | request_rework | request_human_input | finish_turn",
                    "recipient": {
                        "kind": "member | role | room",
                        "member_id": "UUID only for member",
                        "role": "role only for role",
                    },
                    "content": "required text",
                    "artifact_ids": ["UUID"],
                    "reply_to": "UUID required for answer_question",
                }
            ]
        }
        return (
            "You are participating in a controlled CodeCrew task room. "
            "Return only one JSON object matching the action schema. "
            "The last and only terminal action must be finish_turn.\n\n"
            f"Issue:\n{task.issue}\n\n"
            f"Your member_id: {member_id}\n"
            f"Room members:\n{json.dumps(roster, ensure_ascii=False)}\n\n"
            f"New messages:\n{json.dumps(messages, ensure_ascii=False)}\n\n"
            f"Action schema:\n{json.dumps(schema, ensure_ascii=False)}"
        )

    @staticmethod
    def _turn_key(
        member_id: UUID, incoming: tuple[StoredChatMessage, ...]
    ) -> str:
        source = ":".join(
            [str(member_id), *(str(item.message.message_id) for item in incoming)]
        )
        return hashlib.sha256(source.encode()).hexdigest()[:24]
