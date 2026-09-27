import hashlib
import json
import re
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.orchestration.models import Task
from app.storage import Migration
from app.team.models import MessageType
from app.team.store import TeamRoomStore
from app.team.turns import AgentAttemptUsage, AgentTurnResult


class ConversationBudgetCode(str, Enum):
    AGENT_TURNS = "agent_turns"
    REPORTED_TOKENS = "reported_tokens"
    AGENT_DURATION = "agent_duration"
    ROOM_MESSAGES = "room_messages"
    REPEATED_MESSAGE = "repeated_message"
    QUESTIONS_WITHOUT_PROGRESS = "questions_without_progress"


class ConversationBudgetPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_agent_turns: int = Field(default=30, ge=0, le=1000)
    max_reported_tokens: int = Field(default=500_000, ge=0)
    max_agent_duration_ms: int = Field(default=7_200_000, ge=0)
    max_room_messages: int = Field(default=200, ge=1, le=1000)
    max_repeated_messages: int = Field(default=3, ge=1, le=20)
    max_questions_without_progress: int = Field(default=4, ge=1, le=20)

    @classmethod
    def from_settings(cls, settings: Settings) -> "ConversationBudgetPolicy":
        return cls(
            max_agent_turns=settings.max_conversation_agent_turns,
            max_reported_tokens=settings.max_conversation_tokens,
            max_agent_duration_ms=settings.max_conversation_duration_seconds * 1000,
            max_room_messages=settings.max_conversation_messages,
            max_repeated_messages=settings.max_repeated_messages,
            max_questions_without_progress=settings.max_questions_without_progress,
        )


class ConversationBudgetUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_turns: int = Field(ge=0)
    reported_input_tokens: int = Field(ge=0)
    reported_output_tokens: int = Field(ge=0)
    reported_total_tokens: int = Field(ge=0)
    turns_without_token_usage: int = Field(ge=0)
    agent_duration_ms: int = Field(ge=0)
    room_messages: int = Field(ge=0)


class ConversationBudgetViolation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ConversationBudgetCode
    actual: int
    limit: int
    detail: str


CONVERSATION_BUDGET_MIGRATIONS = (
    Migration(
        version=6,
        name="create_agent_turn_usage",
        statements=(
            """
            CREATE TABLE agent_turn_usage (
                session_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                room_id TEXT NOT NULL REFERENCES team_rooms(room_id),
                member_id TEXT NOT NULL REFERENCES room_members(member_id),
                agent_name TEXT NOT NULL,
                role TEXT NOT NULL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cached_input_tokens INTEGER,
                duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX agent_turn_usage_task_idx
            ON agent_turn_usage(task_id, created_at)
            """,
        ),
    ),
)


_PROGRESS_TYPES = {
    MessageType.PLAN_SHARED,
    MessageType.IMPLEMENTATION_READY,
    MessageType.VERIFICATION_READY,
    MessageType.REVIEW_APPROVED,
    MessageType.REWORK_REQUEST,
    MessageType.COMPLETION_PASSED,
}


class ConversationBudgetGuard:
    """Persist Agent usage and deterministically stop unproductive conversations."""

    def __init__(
        self,
        rooms: TeamRoomStore,
        policy: ConversationBudgetPolicy | None = None,
    ) -> None:
        self.rooms = rooms
        self.policy = policy or ConversationBudgetPolicy()

    def initialize(self) -> None:
        self.rooms.database.initialize(CONVERSATION_BUDGET_MIGRATIONS)

    def record_turn(
        self,
        task: Task,
        *,
        room_id: UUID,
        member_id: UUID,
        turn: AgentTurnResult,
    ) -> None:
        self.record_attempt(
            task, room_id=room_id, member_id=member_id,
            agent_name=turn.session.agent_name,
            attempt=AgentAttemptUsage(
                turn.session.session_id, turn.session.started_at,
                turn.agent_result.duration_ms, turn.session, turn.agent_result,
            ),
        )

    def record_attempt(
        self, task: Task, attempt: AgentAttemptUsage, *, room_id: UUID, member_id: UUID,
        agent_name: str,
    ) -> None:
        """Reserve/update one dispatch, including failed starts and unknown costs.

        The legacy session_id column is the stable accounting ID, not proof
        that the provider returned a native session. Updates never erase facts.
        """
        room = self.rooms.get_room(room_id)
        member = self.rooms.get_member(member_id)
        if (room.task_id != task.id or room.trace_id != task.trace_id
                or member.room_id != room_id or member.kind.value != "agent"
                or member.role.value not in {"planner", "implementer", "reviewer"}
                or not agent_name or attempt.duration_ms < 0
                or attempt.started_at.tzinfo is None):
            raise ValueError("Agent accounting scope is invalid")
        session, result = attempt.session, attempt.result
        if session is not None and (
            session.task_id != task.id or session.trace_id != task.trace_id
            or session.agent_name != agent_name or session.role.value != member.role.value
        ):
            raise ValueError("Agent accounting session does not match dispatch")
        if result is not None and (
            session is None or result.session_id != session.session_id
            or result.trace_id != task.trace_id
        ):
            raise ValueError("Agent accounting result does not match session")
        usage = result.token_usage if result else None
        identity = (
            str(task.id), str(task.trace_id), str(room_id), str(member_id),
            agent_name, member.role.value,
        )
        tokens = (
            usage.input_tokens if usage else None,
            usage.output_tokens if usage else None,
            usage.cached_input_tokens if usage else None,
        )
        duration = max(attempt.duration_ms, result.duration_ms if result else 0)
        with self.rooms.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM agent_turn_usage WHERE session_id = ?",
                (str(attempt.attempt_id),),
            ).fetchone()
            if row is not None:
                columns = ("task_id", "trace_id", "room_id", "member_id", "agent_name", "role")
                if (tuple(row[column] for column in columns) != identity
                        or row["created_at"] != attempt.started_at.isoformat()):
                    raise ValueError("Agent accounting ID has conflicting identity")
                token_columns = ("input_tokens", "output_tokens", "cached_input_tokens")
                if any(row[col] is not None and value is not None and row[col] != value
                       for col, value in zip(token_columns, tokens, strict=True)):
                    raise ValueError("Agent accounting has conflicting token facts")
                connection.execute(
                    """UPDATE agent_turn_usage SET
                    input_tokens = COALESCE(input_tokens, ?),
                    output_tokens = COALESCE(output_tokens, ?),
                    cached_input_tokens = COALESCE(cached_input_tokens, ?),
                    duration_ms = MAX(duration_ms, ?) WHERE session_id = ?""",
                    (*tokens, duration, str(attempt.attempt_id)),
                )
                return
            connection.execute(
                """
                INSERT OR IGNORE INTO agent_turn_usage(
                    session_id, task_id, trace_id, room_id, member_id, agent_name,
                    role, input_tokens, output_tokens, cached_input_tokens,
                    duration_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(attempt.attempt_id), *identity, *tokens, duration,
                    attempt.started_at.isoformat(),
                ),
            )

    def usage(self, task_id: UUID, *, room_id: UUID) -> ConversationBudgetUsage:
        with self.rooms.database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS agent_turns,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    SUM(CASE WHEN input_tokens IS NULL OR output_tokens IS NULL THEN 1 ELSE 0 END)
                        AS unknown_tokens,
                    COALESCE(SUM(duration_ms), 0) AS duration_ms
                FROM agent_turn_usage WHERE task_id = ? AND room_id = ?
                """,
                (str(task_id), str(room_id)),
            ).fetchone()
        messages = self.rooms.list_messages(room_id, limit=1_000)
        input_tokens = int(row["input_tokens"])
        output_tokens = int(row["output_tokens"])
        return ConversationBudgetUsage(
            agent_turns=int(row["agent_turns"]),
            reported_input_tokens=input_tokens,
            reported_output_tokens=output_tokens,
            reported_total_tokens=input_tokens + output_tokens,
            turns_without_token_usage=int(row["unknown_tokens"] or 0),
            agent_duration_ms=int(row["duration_ms"]),
            room_messages=len(messages),
        )

    def evaluate(
        self, task_id: UUID, *, room_id: UUID
    ) -> ConversationBudgetViolation | None:
        usage = self.usage(task_id, room_id=room_id)
        checks = (
            (
                ConversationBudgetCode.AGENT_TURNS,
                usage.agent_turns,
                self.policy.max_agent_turns,
                "Agent turn budget exhausted",
            ),
            (
                ConversationBudgetCode.REPORTED_TOKENS,
                usage.reported_total_tokens,
                self.policy.max_reported_tokens,
                "reported Token budget exhausted",
            ),
            (
                ConversationBudgetCode.AGENT_DURATION,
                usage.agent_duration_ms,
                self.policy.max_agent_duration_ms,
                "Agent execution-time budget exhausted",
            ),
            (
                ConversationBudgetCode.ROOM_MESSAGES,
                usage.room_messages,
                self.policy.max_room_messages,
                "room message budget exhausted",
            ),
        )
        for code, actual, limit, detail in checks:
            if actual >= limit:
                return ConversationBudgetViolation(
                    code=code, actual=actual, limit=limit, detail=detail
                )

        messages = self.rooms.list_messages(room_id, limit=1_000)
        repeated = self._max_repeated_count(messages)
        if repeated >= self.policy.max_repeated_messages:
            return ConversationBudgetViolation(
                code=ConversationBudgetCode.REPEATED_MESSAGE,
                actual=repeated,
                limit=self.policy.max_repeated_messages,
                detail="semantically repeated room messages detected",
            )
        questions = self._questions_since_progress(messages)
        if questions >= self.policy.max_questions_without_progress:
            return ConversationBudgetViolation(
                code=ConversationBudgetCode.QUESTIONS_WITHOUT_PROGRESS,
                actual=questions,
                limit=self.policy.max_questions_without_progress,
                detail="too many questions were exchanged without workflow progress",
            )
        return None

    @staticmethod
    def _max_repeated_count(messages: tuple) -> int:
        counts: dict[str, int] = {}
        for stored in messages:
            message = stored.message
            if message.type not in {MessageType.QUESTION, MessageType.ANSWER, MessageType.MESSAGE}:
                continue
            normalized = re.sub(r"\s+", " ", message.content.strip().casefold())
            payload = json.dumps(
                [str(message.sender_id), message.type.value, normalized],
                separators=(",", ":"),
            )
            fingerprint = hashlib.sha256(payload.encode()).hexdigest()
            counts[fingerprint] = counts.get(fingerprint, 0) + 1
        return max(counts.values(), default=0)

    @staticmethod
    def _questions_since_progress(messages: tuple) -> int:
        count = 0
        for stored in messages:
            if stored.message.type in _PROGRESS_TYPES:
                count = 0
            elif stored.message.type is MessageType.QUESTION:
                count += 1
        return count
