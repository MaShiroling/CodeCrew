from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from uuid import UUID

from app.agents.models import (
    AgentCapability,
    AgentEvent,
    AgentRequest,
    AgentResult,
    AgentSession,
)


class AgentAdapterError(RuntimeError):
    """Base error for failures at the provider adapter boundary."""


class AgentSessionNotFoundError(AgentAdapterError):
    """Raised when an adapter does not own the requested local session."""


class AgentAdapter(ABC):
    """Provider-neutral lifecycle contract for one kind of coding agent.

    Adapters report process and provider facts. They never decide whether the
    underlying software-change task has satisfied CodeCrew's completion guard.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the stable adapter name used by the registry."""

    @property
    @abstractmethod
    def capabilities(self) -> frozenset[AgentCapability]:
        """Return capabilities implemented by this adapter."""

    @abstractmethod
    async def start(self, request: AgentRequest) -> AgentSession:
        """Start a new non-blocking session and return its local identity."""

    @abstractmethod
    def stream(self, session_id: UUID) -> AsyncIterator[AgentEvent]:
        """Yield normalized events for a session in adapter-defined order."""

    @abstractmethod
    async def wait(self, session_id: UUID) -> AgentResult:
        """Wait for a session's single final execution result."""

    @abstractmethod
    async def cancel(self, session_id: UUID) -> None:
        """Request cancellation; implementations must make repeated calls safe."""

    @abstractmethod
    async def resume(self, native_session_id: str, request: AgentRequest) -> AgentSession:
        """Resume a provider-native session under a new local session identity."""

    def supports(self, capability: AgentCapability) -> bool:
        return capability in self.capabilities

