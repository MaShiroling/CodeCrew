"""Provider-neutral agent models and adapters."""

from app.agents.base import AgentAdapter, AgentAdapterError, AgentSessionNotFoundError
from app.agents.claude import ClaudeCodeAdapter, DeepSeekClaudeReviewerAdapter
from app.agents.codex import CodexCliAdapter
from app.agents.fake import FakeAgentAdapter, FakeAgentScenario, FakeEventSpec
from app.agents.kimi import KimiCodeAdapter
from app.agents.models import (
    AgentCapability,
    AgentEvent,
    AgentEventType,
    AgentExitReason,
    AgentRequest,
    AgentResult,
    AgentRole,
    AgentSession,
    AgentSessionStatus,
    PermissionMode,
    TokenUsage,
)
from app.agents.process import (
    AsyncProcessRunner,
    ManagedProcess,
    ProcessChunk,
    ProcessResult,
    ProcessRunnerError,
    ProcessStartError,
    ProcessStream,
)
from app.agents.registry import (
    AgentAlreadyRegisteredError,
    AgentAvailability,
    AgentCompatibilityError,
    AgentDescriptor,
    AgentNotRegisteredError,
    AgentRegistry,
    AgentRegistryError,
)

__all__ = [
    "AgentAdapter",
    "AgentAdapterError",
    "AgentAlreadyRegisteredError",
    "AgentAvailability",
    "AgentCapability",
    "AgentCompatibilityError",
    "AgentDescriptor",
    "AgentEvent",
    "AgentEventType",
    "AgentExitReason",
    "AgentNotRegisteredError",
    "AgentRegistry",
    "AgentRegistryError",
    "AgentRequest",
    "AgentResult",
    "AgentRole",
    "AgentSession",
    "AgentSessionNotFoundError",
    "AgentSessionStatus",
    "AsyncProcessRunner",
    "ClaudeCodeAdapter",
    "CodexCliAdapter",
    "DeepSeekClaudeReviewerAdapter",
    "FakeAgentAdapter",
    "FakeAgentScenario",
    "FakeEventSpec",
    "KimiCodeAdapter",
    "ManagedProcess",
    "PermissionMode",
    "ProcessChunk",
    "ProcessResult",
    "ProcessRunnerError",
    "ProcessStartError",
    "ProcessStream",
    "TokenUsage",
]
