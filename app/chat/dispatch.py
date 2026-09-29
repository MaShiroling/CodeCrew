"""Bounded, message-driven read-only discussion; never starts a coding task."""

import asyncio
import json
from collections.abc import Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents import AgentExitReason
from app.chat.agents import StandaloneChatAgentRuntime
from app.chat.models import (
    ChatTurnStatus,
    StandaloneChatMessage,
    StandaloneChatTurn,
    StoredStandaloneChatMessage,
)
from app.chat.store import StandaloneChatStore
from app.team.models import MAX_CHAT_CONTENT_CHARS, MemberRole, RoomStatus
from app.team.personas import default_team_personas

_AGENT_ROLES = frozenset({MemberRole.PLANNER, MemberRole.IMPLEMENTER, MemberRole.REVIEWER})
_CHAT_RESPONSIBILITIES = {
    MemberRole.PLANNER: "讨论需求边界、方案取舍和验收条件",
    MemberRole.IMPLEMENTER: "讨论实现可行性、兼容性和测试边界",
    MemberRole.REVIEWER: "讨论潜在风险、证据缺口和验证建议",
}


class _ChatReply(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    content: str = Field(min_length=1, max_length=MAX_CHAT_CONTENT_CHARS)
    handoff_to: tuple[MemberRole, ...] = Field(default=(), max_length=2)


def _normalize_reply(output: Mapping[str, object], *, own_role: MemberRole) -> _ChatReply:
    raw = output.get("structured_output", output.get("message", output.get("result")))
    if isinstance(raw, dict):
        payload = raw
    elif isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("```json") and stripped.endswith("```"):
            stripped = stripped[7:-3].strip()
        if stripped.startswith("{"):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError("Agent returned invalid discussion JSON") from exc
        else:
            payload = {"content": stripped}
    else:
        raise TypeError("Agent returned no discussion message")
    try:
        reply = _ChatReply.model_validate(payload)
    except ValidationError as exc:
        raise ValueError("Agent returned an invalid discussion reply") from exc
    if not reply.content.strip() or len(set(reply.handoff_to)) != len(reply.handoff_to):
        raise ValueError("Agent discussion content or recipients are invalid")
    if any(role not in _AGENT_ROLES or role is own_role for role in reply.handoff_to):
        raise ValueError("Agent handoff must target other chat Agents")
    return reply


class StandaloneChatDispatcher:
    """Each delivery gets one durable claim; no implicit replay after restart."""

    def __init__(
        self, store: StandaloneChatStore, runtime: StandaloneChatAgentRuntime,
        *, max_turns_per_thread: int = 6, timeout_seconds: int = 180,
    ) -> None:
        if max_turns_per_thread < 1 or timeout_seconds < 1:
            raise ValueError("chat limits must be positive")
        self.store = store
        self.runtime = runtime
        self.max_turns_per_thread = max_turns_per_thread
        self.timeout_seconds = timeout_seconds
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._sessions: dict[UUID, UUID] = {}
        self._stopping = False

    async def startup(self) -> None:
        # The CLI process from an old server cannot be safely adopted. Fence all
        # unfinished durable claims, including work queued but not yet launched.
        self.store.interrupt_unfinished_turns()
        self._stopping = False

    async def shutdown(self) -> None:
        self._stopping = True
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.store.interrupt_unfinished_turns()

    def enqueue(self, stored: StoredStandaloneChatMessage) -> tuple[StandaloneChatTurn, ...]:
        """Reserve mentioned Agent deliveries; idempotent replays never re-launch."""
        if self._stopping:
            return ()
        room = self.store.get_room(stored.message.room_id)
        if room.status is RoomStatus.CLOSED:
            return ()
        roles = {member.member_id: member.role for member in room.members}
        claims = []
        for recipient_id in stored.message.recipient_ids:
            if roles[recipient_id] not in _AGENT_ROLES:
                continue
            claim, created = self.store.claim_turn(
                stored.message.message_id, recipient_id,
                max_turns=self.max_turns_per_thread,
            )
            claims.append(claim)
            if created:
                task = asyncio.create_task(self._execute(claim.turn_id))
                self._tasks[claim.turn_id] = task
                task.add_done_callback(lambda _done, turn_id=claim.turn_id: self._tasks.pop(turn_id, None))
        return tuple(claims)

    async def cancel(self, turn_id: UUID) -> StandaloneChatTurn:
        turn = self.store.get_turn(turn_id)
        if turn.status in {ChatTurnStatus.QUEUED, ChatTurnStatus.RUNNING}:
            task = self._tasks.get(turn_id)
            if task is None:
                self.store.transition_turn(
                    turn_id, from_status=turn.status, to_status=ChatTurnStatus.INTERRUPTED,
                    error="no live controller owns this turn",
                )
            else:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                current = self.store.get_turn(turn_id)
                if current.status is ChatTurnStatus.QUEUED:
                    self.store.transition_turn(
                        turn_id, from_status=ChatTurnStatus.QUEUED,
                        to_status=ChatTurnStatus.CANCELLED,
                    )
                elif current.status is ChatTurnStatus.RUNNING:
                    self.store.transition_turn(
                        turn_id, from_status=ChatTurnStatus.RUNNING,
                        to_status=ChatTurnStatus.INTERRUPTED,
                        error="Agent cancellation was not confirmed",
                    )
        return self.store.get_turn(turn_id)

    async def wait_idle(self) -> None:
        """Wait for this controller's finite fanout; useful for deterministic tests."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks.values()), return_exceptions=True)

    async def _execute(self, turn_id: UUID) -> None:
        session_id: UUID | None = None
        result_confirmed = False
        turn = self.store.get_turn(turn_id)
        try:
            if not self.store.transition_turn(
                turn_id, from_status=ChatTurnStatus.QUEUED,
                to_status=ChatTurnStatus.RUNNING,
            ):
                return
            stored = self.store.get_message(turn.message_id)
            room = self.store.get_room(turn.room_id)
            recipient = next(member for member in room.members if member.member_id == turn.recipient_id)
            sender = next(member for member in room.members if member.member_id == stored.message.sender_id)
            prompt = self._prompt(recipient.role, sender.name, stored.message.content)
            session = await self.runtime.start(
                room_id=room.room_id, trace_id=room.trace_id, role=recipient.role,
                prompt=prompt, timeout_seconds=self.timeout_seconds,
            )
            session_id = session.session_id
            self._sessions[turn_id] = session_id
            self.store.transition_turn(
                turn_id, from_status=ChatTurnStatus.RUNNING,
                to_status=ChatTurnStatus.RUNNING, session_id=session_id,
            )
            async for _event in self.runtime.stream(session_id):
                pass
            result = await self.runtime.wait(session_id)
            result_confirmed = True
            self._sessions.pop(turn_id, None)
            session_id = None
            if result.reason is not AgentExitReason.COMPLETED or result.exit_code not in {0, None}:
                raise RuntimeError(f"Agent turn ended: {result.reason.value}")
            reply = _normalize_reply(result.output, own_role=recipient.role)
            human = next(member for member in room.members if member.role is MemberRole.HUMAN)
            teammate_ids = tuple(
                next(member.member_id for member in room.members if member.role is role)
                for role in reply.handoff_to
            )
            response = self.store.append_message(StandaloneChatMessage(
                room_id=room.room_id, trace_id=room.trace_id,
                sender_id=recipient.member_id,
                recipient_ids=(human.member_id, *teammate_ids),
                content=reply.content, reply_to=stored.message.message_id,
                causation_id=stored.message.message_id,
                correlation_id=stored.message.correlation_id,
                idempotency_key=f"chat-turn:{turn_id}",
            ))
            self.store.acknowledge(stored.message.message_id, recipient_id=recipient.member_id)
            self.store.transition_turn(
                turn_id, from_status=ChatTurnStatus.RUNNING,
                to_status=ChatTurnStatus.SUCCEEDED,
            )
            self.enqueue(response)
        except asyncio.CancelledError:
            if session_id is not None:
                try:
                    await asyncio.wait_for(self.runtime.cancel(session_id), timeout=10)
                except Exception:  # noqa: BLE001 - unknown CLI termination must be fenced
                    self.store.transition_turn(
                        turn_id, from_status=ChatTurnStatus.RUNNING,
                        to_status=ChatTurnStatus.INTERRUPTED,
                        error="Agent cancellation was not confirmed",
                    )
                else:
                    self.store.transition_turn(
                        turn_id, from_status=ChatTurnStatus.RUNNING,
                        to_status=ChatTurnStatus.CANCELLED,
                    )
            else:
                current = self.store.get_turn(turn_id)
                self.store.transition_turn(
                    turn_id, from_status=current.status,
                    to_status=(ChatTurnStatus.CANCELLED if current.status is ChatTurnStatus.QUEUED
                               else ChatTurnStatus.INTERRUPTED),
                    error=(None if current.status is ChatTurnStatus.QUEUED
                           else "Agent start or result status is unknown"),
                )
        except Exception as exc:  # noqa: BLE001 - capture external CLI/store boundary failures
            terminal = ChatTurnStatus.FAILED if result_confirmed else ChatTurnStatus.INTERRUPTED
            if session_id is not None:
                try:
                    await asyncio.wait_for(self.runtime.cancel(session_id), timeout=10)
                except Exception:  # noqa: BLE001 - uncertain process state must remain visible
                    terminal = ChatTurnStatus.INTERRUPTED
                else:
                    terminal = ChatTurnStatus.FAILED
            self.store.transition_turn(
                turn_id, from_status=ChatTurnStatus.RUNNING,
                to_status=terminal, error=f"chat turn failed at {type(exc).__name__}",
            )
        finally:
            self._sessions.pop(turn_id, None)

    @staticmethod
    def _prompt(role: MemberRole, sender: str, content: str) -> str:
        persona = default_team_personas().for_role(role)
        return (
            "你正在独立的纯聊天房间，不关联 Git 仓库或编码任务。仅讨论，不读写代码、"
            "不运行命令、不创建任务、不宣称实现或测试已完成。\n"
            f"你的身份：{persona.display_name}；讨论职责：{_CHAT_RESPONSIBILITIES[role]}"
            f"；风格：{persona.personality}\n"
            "只根据下面这一条收到的消息作答，不假设你看过其他对话。\n"
            "最终只输出一个 JSON 对象："
            '{"content":"给人的回复","handoff_to":[]}。'
            "handoff_to 可以填其他成员的角色 planner、implementer、reviewer，至多两位；"
            "仅确需队友回答时使用，不要提及自己。\n"
            f"发送者：{sender}\n收到的消息：{content}"
        )
