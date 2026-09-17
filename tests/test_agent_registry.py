import asyncio

import pytest

from app.agents import (
    AgentAlreadyRegisteredError,
    AgentAvailability,
    AgentCapability,
    AgentCompatibilityError,
    AgentNotRegisteredError,
    AgentRegistry,
    AgentRegistryError,
    AgentRole,
    FakeAgentAdapter,
    PermissionMode,
)


def make_registry(*, max_concurrency: int = 1) -> tuple[AgentRegistry, FakeAgentAdapter]:
    registry = AgentRegistry()
    adapter = FakeAgentAdapter(
        name="planner",
        capabilities=frozenset(
            {AgentCapability.REPOSITORY_ANALYSIS, AgentCapability.STREAMING}
        ),
    )
    registry.register(
        adapter,
        roles={AgentRole.PLANNER, AgentRole.REVIEWER},
        permission_modes={PermissionMode.READ_ONLY},
        max_concurrency=max_concurrency,
    )
    return registry, adapter


def test_register_resolve_describe_and_list() -> None:
    registry, adapter = make_registry(max_concurrency=2)

    resolved = registry.resolve(
        "planner",
        role=AgentRole.PLANNER,
        permission_mode=PermissionMode.READ_ONLY,
        required_capabilities={AgentCapability.REPOSITORY_ANALYSIS},
    )
    descriptor = registry.describe("planner")

    assert resolved is adapter
    assert descriptor.max_concurrency == 2
    assert descriptor.active_sessions == 0
    assert descriptor.queued_sessions == 0
    assert descriptor.availability is AgentAvailability.AVAILABLE
    assert registry.list() == (descriptor,)


def test_duplicate_and_unknown_agents_are_rejected() -> None:
    registry, adapter = make_registry()

    with pytest.raises(AgentAlreadyRegisteredError, match="already registered"):
        registry.register(adapter, roles={AgentRole.PLANNER})

    with pytest.raises(AgentNotRegisteredError, match="not registered"):
        registry.describe("missing")


@pytest.mark.parametrize(
    ("role", "permission", "capabilities", "message"),
    [
        (
            AgentRole.IMPLEMENTER,
            PermissionMode.READ_ONLY,
            (),
            "does not support role",
        ),
        (
            AgentRole.PLANNER,
            PermissionMode.WORKSPACE_WRITE,
            (),
            "does not allow permission mode",
        ),
        (
            AgentRole.PLANNER,
            PermissionMode.READ_ONLY,
            (AgentCapability.CODE_EDIT,),
            "lacks capabilities: code_edit",
        ),
    ],
)
def test_resolve_enforces_compatibility(
    role: AgentRole,
    permission: PermissionMode,
    capabilities: tuple[AgentCapability, ...],
    message: str,
) -> None:
    registry, _ = make_registry()

    with pytest.raises(AgentCompatibilityError, match=message):
        registry.resolve(
            "planner",
            role=role,
            permission_mode=permission,
            required_capabilities=capabilities,
        )


@pytest.mark.asyncio
async def test_concurrency_limit_blocks_until_lease_is_released() -> None:
    registry, adapter = make_registry(max_concurrency=1)
    first_acquired = asyncio.Event()
    release_first = asyncio.Event()
    second_acquired = asyncio.Event()

    async def first_user() -> None:
        async with registry.acquire(
            "planner",
            role=AgentRole.PLANNER,
            permission_mode=PermissionMode.READ_ONLY,
        ) as leased:
            assert leased is adapter
            first_acquired.set()
            await release_first.wait()

    async def second_user() -> None:
        await first_acquired.wait()
        async with registry.acquire(
            "planner",
            role=AgentRole.REVIEWER,
            permission_mode=PermissionMode.READ_ONLY,
        ):
            second_acquired.set()

    first_task = asyncio.create_task(first_user())
    second_task = asyncio.create_task(second_user())
    await first_acquired.wait()
    await asyncio.sleep(0)

    assert registry.describe("planner").availability is AgentAvailability.BUSY
    assert registry.describe("planner").queued_sessions == 1
    assert not second_acquired.is_set()

    release_first.set()
    await asyncio.gather(first_task, second_task)

    assert second_acquired.is_set()
    assert registry.describe("planner").active_sessions == 0


@pytest.mark.asyncio
async def test_lease_is_released_when_consumer_raises() -> None:
    registry, _ = make_registry()

    with pytest.raises(RuntimeError, match="consumer failed"):
        async with registry.acquire(
            "planner",
            role=AgentRole.PLANNER,
            permission_mode=PermissionMode.READ_ONLY,
        ):
            raise RuntimeError("consumer failed")

    assert registry.describe("planner").availability is AgentAvailability.AVAILABLE


@pytest.mark.asyncio
async def test_active_agent_cannot_be_unregistered() -> None:
    registry, adapter = make_registry()

    async with registry.acquire(
        "planner",
        role=AgentRole.PLANNER,
        permission_mode=PermissionMode.READ_ONLY,
    ):
        with pytest.raises(AgentRegistryError, match="cannot unregister in-use"):
            registry.unregister("planner")

    assert registry.unregister("planner") is adapter
    with pytest.raises(AgentNotRegisteredError):
        registry.describe("planner")


@pytest.mark.parametrize(
    ("roles", "permissions", "max_concurrency", "message"),
    [
        (set(), {PermissionMode.READ_ONLY}, 1, "roles must not be empty"),
        ({AgentRole.PLANNER}, set(), 1, "permission_modes must not be empty"),
        ({AgentRole.PLANNER}, {PermissionMode.READ_ONLY}, 0, "must be positive"),
    ],
)
def test_invalid_registration_is_rejected(
    roles: set[AgentRole],
    permissions: set[PermissionMode],
    max_concurrency: int,
    message: str,
) -> None:
    registry = AgentRegistry()

    with pytest.raises(ValueError, match=message):
        registry.register(
            FakeAgentAdapter(),
            roles=roles,
            permission_modes=permissions,
            max_concurrency=max_concurrency,
        )
