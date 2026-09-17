import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.agents.base import AgentAdapter
from app.agents.models import AgentCapability, AgentRole, PermissionMode


class AgentRegistryError(RuntimeError):
    """Base error for agent registration and routing failures."""


class AgentAlreadyRegisteredError(AgentRegistryError):
    pass


class AgentNotRegisteredError(AgentRegistryError):
    pass


class AgentCompatibilityError(AgentRegistryError):
    pass


class AgentAvailability(str, Enum):
    AVAILABLE = "available"
    BUSY = "busy"


class AgentDescriptor(BaseModel):
    """Read-only registry view used by orchestration and APIs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    roles: frozenset[AgentRole] = Field(min_length=1)
    capabilities: frozenset[AgentCapability]
    permission_modes: frozenset[PermissionMode] = Field(min_length=1)
    max_concurrency: int = Field(gt=0)
    active_sessions: int = Field(ge=0)
    queued_sessions: int = Field(ge=0)
    availability: AgentAvailability


@dataclass(slots=True)
class _RegistryEntry:
    adapter: AgentAdapter
    roles: frozenset[AgentRole]
    permission_modes: frozenset[PermissionMode]
    max_concurrency: int
    semaphore: asyncio.Semaphore
    active_sessions: int = 0
    queued_sessions: int = 0


class AgentRegistry:
    """In-process adapter registry with capability checks and concurrency leases."""

    def __init__(self) -> None:
        self._entries: dict[str, _RegistryEntry] = {}

    def register(
        self,
        adapter: AgentAdapter,
        *,
        roles: Iterable[AgentRole],
        permission_modes: Iterable[PermissionMode] = (PermissionMode.READ_ONLY,),
        max_concurrency: int = 1,
    ) -> None:
        if adapter.name in self._entries:
            raise AgentAlreadyRegisteredError(f"agent {adapter.name!r} is already registered")
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")

        role_set = frozenset(roles)
        permission_set = frozenset(permission_modes)
        if not role_set:
            raise ValueError("roles must not be empty")
        if not permission_set:
            raise ValueError("permission_modes must not be empty")

        self._entries[adapter.name] = _RegistryEntry(
            adapter=adapter,
            roles=role_set,
            permission_modes=permission_set,
            max_concurrency=max_concurrency,
            semaphore=asyncio.Semaphore(max_concurrency),
        )

    def unregister(self, name: str) -> AgentAdapter:
        entry = self._get_entry(name)
        if entry.active_sessions or entry.queued_sessions:
            raise AgentRegistryError(f"cannot unregister in-use agent {name!r}")
        del self._entries[name]
        return entry.adapter

    def resolve(
        self,
        name: str,
        *,
        role: AgentRole,
        permission_mode: PermissionMode,
        required_capabilities: Iterable[AgentCapability] = (),
    ) -> AgentAdapter:
        entry = self._get_entry(name)
        if role not in entry.roles:
            raise AgentCompatibilityError(
                f"agent {name!r} does not support role {role.value!r}"
            )
        if permission_mode not in entry.permission_modes:
            raise AgentCompatibilityError(
                f"agent {name!r} does not allow permission mode {permission_mode.value!r}"
            )

        missing = frozenset(required_capabilities) - entry.adapter.capabilities
        if missing:
            values = ", ".join(sorted(capability.value for capability in missing))
            raise AgentCompatibilityError(f"agent {name!r} lacks capabilities: {values}")
        return entry.adapter

    def describe(self, name: str) -> AgentDescriptor:
        entry = self._get_entry(name)
        availability = (
            AgentAvailability.BUSY
            if entry.active_sessions >= entry.max_concurrency
            else AgentAvailability.AVAILABLE
        )
        return AgentDescriptor(
            name=name,
            roles=entry.roles,
            capabilities=entry.adapter.capabilities,
            permission_modes=entry.permission_modes,
            max_concurrency=entry.max_concurrency,
            active_sessions=entry.active_sessions,
            queued_sessions=entry.queued_sessions,
            availability=availability,
        )

    def list(self) -> tuple[AgentDescriptor, ...]:
        return tuple(self.describe(name) for name in sorted(self._entries))

    @asynccontextmanager
    async def acquire(
        self,
        name: str,
        *,
        role: AgentRole,
        permission_mode: PermissionMode,
        required_capabilities: Iterable[AgentCapability] = (),
    ) -> AsyncIterator[AgentAdapter]:
        adapter = self.resolve(
            name,
            role=role,
            permission_mode=permission_mode,
            required_capabilities=required_capabilities,
        )
        entry = self._get_entry(name)
        entry.queued_sessions += 1
        try:
            await entry.semaphore.acquire()
        finally:
            entry.queued_sessions -= 1
        entry.active_sessions += 1
        try:
            yield adapter
        finally:
            entry.active_sessions -= 1
            entry.semaphore.release()

    def _get_entry(self, name: str) -> _RegistryEntry:
        try:
            return self._entries[name]
        except KeyError as exc:
            raise AgentNotRegisteredError(f"agent {name!r} is not registered") from exc
