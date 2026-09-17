from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskState(str, Enum):
    CREATED = "created"
    PLANNING = "planning"
    IMPLEMENTING = "implementing"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    REWORK = "rework"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_HUMAN = "needs_human"


TERMINAL_STATES = {
    TaskState.COMPLETED,
    TaskState.FAILED,
    TaskState.CANCELLED,
    TaskState.NEEDS_HUMAN,
}

ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.CREATED: frozenset({TaskState.PLANNING, TaskState.CANCELLED}),
    TaskState.PLANNING: frozenset({TaskState.IMPLEMENTING, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.IMPLEMENTING: frozenset({TaskState.VERIFYING, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.VERIFYING: frozenset({TaskState.REVIEWING, TaskState.REWORK, TaskState.FAILED}),
    TaskState.REVIEWING: frozenset({TaskState.COMPLETED, TaskState.REWORK, TaskState.FAILED}),
    TaskState.REWORK: frozenset({TaskState.IMPLEMENTING, TaskState.NEEDS_HUMAN, TaskState.CANCELLED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
    TaskState.NEEDS_HUMAN: frozenset(),
}


class InvalidTaskTransition(ValueError):
    pass


class Task(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    trace_id: UUID = Field(default_factory=uuid4)
    issue: str = Field(min_length=1)
    repository_path: str = Field(min_length=1)
    state: TaskState = TaskState.CREATED
    rework_rounds: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def transition_to(self, target: TaskState) -> None:
        if target not in ALLOWED_TRANSITIONS[self.state]:
            raise InvalidTaskTransition(f"cannot transition task from {self.state} to {target}")
        self.state = target
        self.updated_at = utc_now()

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

