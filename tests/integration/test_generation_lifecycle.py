from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from chassis import CapabilityNotFound, Harness, PluginContext, plugin
from chassis.capabilities import CapabilityKey, ScopedCapabilities
from chassis.core.errors import HarnessStateError
from chassis.plugins.lifecycle import PluginInstance, PluginState
from chassis.runtime import AgentEvent, AgentRequest, AgentResult, HarnessRunContext

DATABASE = CapabilityKey("database", "1")
MEMORY = CapabilityKey("memory", "1")


class Guard:
    """Provider payload that complains if it is used after its scope closed."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.disposed = False

    def use(self) -> str:
        if self.disposed:
            raise RuntimeError(f"{self.name} used after dispose")
        return self.name


def guard_plugin(name: str, *, version: str = "1.0.0", capability: CapabilityKey = DATABASE):  # type: ignore[no-untyped-def]
    @plugin(name=name, version=version, provides={capability.name: version})
    async def guard(ctx: PluginContext) -> None:
        value = Guard(name)
        capabilities: ScopedCapabilities = ctx.capabilities
        capabilities.provide(capability, value, version=version)
        ctx.cleanup(f"{name} disposed", lambda: setattr(value, "disposed", True))

    return guard


def mounted(harness: Harness, entry_id: str) -> PluginInstance:
    instance = harness.plugin_registry.instance(entry_id)
    assert instance is not None, f"entry {entry_id!r} is not mounted"
    return instance


async def test_reconcile_publishes_an_immutable_generation() -> None:
    harness = Harness()
    harness.install(guard_plugin("provider-a"), entry_id="db")
    await harness.start()
    try:
        generation = harness.current_generation
        assert generation is not None
        assert generation.generation_id == "gen_0001"
        assert generation.state.value == "active"
        assert generation.snapshot.require(DATABASE).name == "provider-a"
        assert generation.plugin_versions() == {"provider-a": "1.0.0"}
        assert mounted(harness, "db").generation_refs == 1

        async with harness.acquire() as acquired:
            assert acquired is generation
            assert generation.lease_count == 1

        assert generation.lease_count == 0
    finally:
        await harness.stop()


async def test_reconcile_of_unchanged_state_does_not_churn_generations() -> None:
    harness = Harness()
    harness.install(guard_plugin("provider-a"), entry_id="db")
    await harness.start()
    try:
        first = harness.current_generation

        result = await harness.reconcile()

        assert harness.current_generation is first
        assert result.generation_id == first.generation_id  # type: ignore[union-attr]
        assert result.mounted == ()
        assert harness.generation_manager.history == ()
    finally:
        await harness.stop()


async def test_provider_replacement_leaves_active_runs_on_their_generation() -> None:
    harness = Harness()
    harness.install(guard_plugin("provider-a"), entry_id="db")
    await harness.start()
    try:
        generation_one = harness.current_generation
        assert generation_one is not None
        provider_a = generation_one.snapshot.require(DATABASE)
        instance_a = mounted(harness, "db")

        started = asyncio.Event()
        finish = asyncio.Event()

        async def run_a() -> str:
            async with harness.acquire() as generation:
                provider = generation.snapshot.require(DATABASE)
                started.set()
                await finish.wait()
                return str(provider.use())

        run = asyncio.create_task(run_a())
        await started.wait()

        harness.install(guard_plugin("provider-b"), entry_id="db", replace=True)
        result = await harness.reconcile()
        generation_two = harness.current_generation
        assert generation_two is not None
        assert generation_two.generation_id != generation_one.generation_id
        assert generation_two.snapshot.require(DATABASE).name == "provider-b"

        # Run A keeps the provider it acquired, and that provider is still alive
        # because generation one can still reach it.
        assert provider_a.disposed is False
        assert instance_a.state is PluginState.ACTIVE
        assert instance_a.generation_refs == 1
        assert result.generation_id == generation_two.generation_id

        async with harness.acquire() as run_b_generation:
            assert run_b_generation is generation_two
            assert run_b_generation.snapshot.require(DATABASE).name == "provider-b"

        finish.set()
        assert await run == "provider-a"

        # Run A finishing drains generation one, which makes provider A
        # unreachable and therefore disposable.
        assert provider_a.disposed is True
        assert instance_a.state is PluginState.DISPOSED
        assert mounted(harness, "db").state is PluginState.ACTIVE
    finally:
        await harness.stop()


async def test_snapshot_observed_by_a_run_never_changes() -> None:
    harness = Harness()
    harness.install(guard_plugin("provider-a"), entry_id="db")
    await harness.start()
    try:
        generation_one = harness.current_generation
        assert generation_one is not None
        snapshot = generation_one.snapshot

        harness.install(guard_plugin("provider-b"), entry_id="db", replace=True)
        await harness.reconcile()

        assert snapshot.require(DATABASE).name == "provider-a"
        assert harness.current_generation is not None
        assert harness.current_generation.snapshot.require(DATABASE).name == "provider-b"
        with pytest.raises(CapabilityNotFound):
            snapshot.require(MEMORY)
    finally:
        await harness.stop()


async def test_shared_plugin_survives_until_every_generation_releases_it() -> None:
    harness = Harness()
    harness.install(guard_plugin("provider-a"), entry_id="db")
    await harness.start()
    try:
        generation_one = harness.current_generation
        assert generation_one is not None
        shared = mounted(harness, "db")

        started = asyncio.Event()
        finish = asyncio.Event()

        async def run_a() -> None:
            async with harness.acquire():
                started.set()
                await finish.wait()

        run = asyncio.create_task(run_a())
        await started.wait()

        # Generation two reuses the same database instance and adds a consumer.
        @plugin(
            name="memory",
            version="1.0.0",
            provides={"memory": "1.0.0"},
            requires={"database": ">=1,<2"},
        )
        async def memory(ctx: PluginContext) -> None:
            ctx.capabilities.provide(MEMORY, Guard("memory"))

        harness.install(memory, entry_id="memory")
        await harness.reconcile()

        assert harness.plugin_registry.instance("db") is shared
        assert shared.generation_refs == 2

        finish.set()
        await run

        # Retiring generation one must not dispose the shared instance.
        assert shared.generation_refs == 1
        assert shared.state is PluginState.ACTIVE
        assert shared.scope.is_open
    finally:
        await harness.stop()


async def test_disposal_waits_for_the_last_lease_of_a_removed_plugin() -> None:
    harness = Harness()
    harness.install(guard_plugin("provider-a"), entry_id="db")
    await harness.start()
    try:
        instance = mounted(harness, "db")
        provider = harness.current_generation.snapshot.require(DATABASE)  # type: ignore[union-attr]

        release = asyncio.Event()
        entered = asyncio.Event()

        async def run() -> None:
            async with harness.acquire():
                entered.set()
                await release.wait()

        task = asyncio.create_task(run())
        await entered.wait()

        harness.uninstall("db")
        result = await harness.reconcile()

        assert result.disposed == ()
        assert provider.disposed is False
        assert instance.state is PluginState.ACTIVE

        release.set()
        await task

        assert provider.disposed is True
        assert instance.state is PluginState.DISPOSED
    finally:
        await harness.stop()


async def test_a_leased_generation_survives_history_pruning() -> None:
    """Reachability, not the diagnostics buffer, decides when a plugin is disposed."""

    def extra_plugin(index: int):  # type: ignore[no-untyped-def]
        capability = CapabilityKey(f"extra-{index}", "1")

        @plugin(name=f"extra-{index}", version="1.0.0", provides={capability.name: "1.0.0"})
        async def extra(ctx: PluginContext) -> None:
            ctx.capabilities.provide(capability, Guard(f"extra-{index}"))

        return extra

    harness = Harness(generation_history_limit=1)
    harness.install(guard_plugin("provider-a"), entry_id="db")
    await harness.start()
    try:
        instance = mounted(harness, "db")
        generation_one = harness.current_generation
        assert generation_one is not None
        provider = generation_one.snapshot.require(DATABASE)

        release = asyncio.Event()
        entered = asyncio.Event()

        async def run() -> None:
            async with harness.acquire():
                entered.set()
                await release.wait()

        task = asyncio.create_task(run())
        await entered.wait()

        # Publish far more generations than the history buffer retains, with the
        # leased generation no longer part of the desired state.
        harness.uninstall("db")
        for index in range(4):
            harness.install(extra_plugin(index), entry_id=f"extra-{index}")
            await harness.reconcile()

        assert provider.disposed is False
        assert instance.state is PluginState.ACTIVE
        assert generation_one in harness.generation_manager.live()

        release.set()
        await task

        assert provider.disposed is True
        assert instance.state is PluginState.DISPOSED
    finally:
        await harness.stop()


async def test_acquire_requires_a_running_harness() -> None:
    harness = Harness()

    with pytest.raises(HarnessStateError):
        async with harness.acquire():
            pass  # pragma: no cover

    harness.install(guard_plugin("provider-a"), entry_id="db")
    await harness.start()
    await harness.stop()

    with pytest.raises(HarnessStateError):
        async with harness.acquire():
            pass  # pragma: no cover


async def test_data_plane_is_not_blocked_by_an_in_flight_reconcile() -> None:
    mount_started = asyncio.Event()
    mount_release = asyncio.Event()

    @plugin(name="slow", version="1.0.0", provides={"memory": "1.0.0"})
    async def slow(ctx: PluginContext) -> None:
        mount_started.set()
        await mount_release.wait()
        ctx.capabilities.provide(MEMORY, Guard("slow"))

    harness = Harness()
    harness.install(guard_plugin("provider-a"), entry_id="db")
    await harness.start()
    try:
        harness.install(slow, entry_id="slow")
        reconciling = asyncio.ensure_future(harness.reconcile())
        await mount_started.wait()

        # The control plane is busy mounting, yet a run acquires immediately.
        async with harness.acquire() as generation:
            assert generation.snapshot.require(DATABASE).name == "provider-a"
            assert harness.plugin_registry.instance("slow") is not None

        mount_release.set()
        await reconciling

        assert harness.current_generation is not None
        assert harness.current_generation.snapshot.require(MEMORY).name == "slow"
    finally:
        await harness.stop()


async def test_shutdown_drains_active_runs_before_disposing() -> None:
    harness = Harness(name="graceful", shutdown_grace_seconds=5.0)
    harness.install(guard_plugin("provider-a"), entry_id="db")
    await harness.start()

    instance = mounted(harness, "db")
    provider = harness.current_generation.snapshot.require(DATABASE)  # type: ignore[union-attr]

    release = asyncio.Event()
    entered = asyncio.Event()
    observed: list[bool] = []

    async def run() -> None:
        async with harness.acquire():
            entered.set()
            await release.wait()
            observed.append(provider.disposed)

    task = asyncio.create_task(run())
    await entered.wait()

    stopping = asyncio.ensure_future(harness.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    await task
    await stopping

    assert observed == [False]
    assert provider.disposed is True
    assert instance.state is PluginState.DISPOSED
    assert harness.state.value == "stopped"


async def test_generation_provided_policy_governs_its_own_runs() -> None:
    """A runtime-bound policy is resolved from the generation, not the harness default."""

    from langchain_core.tools import StructuredTool
    from pydantic import create_model

    from chassis.capabilities import POLICY
    from chassis.policy import GrantPolicy
    from chassis.tools import ToolPolicy, ToolRequest

    decisions: list[str] = []

    class RecordingPolicy(GrantPolicy):
        async def evaluate(self, request):  # type: ignore[no-untyped-def]
            decisions.append(str(request.permission))
            return await super().evaluate(request)

    @plugin(name="policy-provider", version="1.0.0", provides={"policy": "1.0.0"})
    async def policy_provider(ctx: PluginContext) -> None:
        ctx.capabilities.provide(POLICY, RecordingPolicy(["network.fetch"]))

    @plugin(name="fetcher", version="1.0.0")
    async def fetcher(ctx: PluginContext) -> None:
        schema = create_model("FetchArgs", url=(str, ...))

        async def run(**kwargs: object) -> str:
            return "fetched"

        ctx.tools.register(
            StructuredTool(
                name="fetch", description="Fetch a URL", args_schema=schema, coroutine=run
            ),
            policy=ToolPolicy(permissions=("network.fetch",)),
        )

    harness = Harness()
    harness.install(policy_provider, entry_id="policy")
    harness.install(fetcher, entry_id="fetcher")
    await harness.start()
    try:
        generation = harness.current_generation
        assert generation is not None
        environment = harness.run_environment(generation)
        assert isinstance(environment.policy, RecordingPolicy)

        result = await environment.executor.execute(
            ToolRequest(name="fetch", args={"url": "https://example.test"}),
            snapshot=harness.tool_snapshot(generation),
            policy=environment.policy,
        )

        assert result.content == "fetched"
        assert decisions == ["network.fetch"]
    finally:
        await harness.stop()


async def test_hot_replacement_keeps_each_generations_own_tools_under_a_lease() -> None:
    from langchain_core.tools import tool as langchain_tool

    def fetcher(version: str):  # type: ignore[no-untyped-def]
        @langchain_tool
        def fetch(url: str) -> str:
            """Fetch a URL."""

            return version

        @plugin(name=f"fetcher-{version}", version="1.0.0")
        async def provide(ctx: PluginContext) -> None:
            ctx.tools.register(fetch)

        return provide

    harness = Harness()
    harness.install(fetcher("v1"), entry_id="fetcher")
    await harness.start()

    async with harness.acquire() as old_generation:
        first = harness.tool_snapshot(old_generation).require("fetch")

        harness.install(fetcher("v2"), entry_id="fetcher", replace=True)
        await harness.reconcile()

        new_generation = harness.current_generation
        assert new_generation is not None
        second = harness.tool_snapshot(new_generation).require("fetch")

        # Old and new generations reference same-named registrations
        # concurrently, each selected by its own instance identity.
        assert harness.tool_snapshot(old_generation).require("fetch") is first
        assert second is not first
        assert await first.tool.ainvoke({"url": "x"}) == "v1"
        assert await second.tool.ainvoke({"url": "x"}) == "v2"

    await harness.stop()


class BlockingRuntime:
    """Named runtime whose run holds a barrier, proving hot replacement."""

    def __init__(self, name: str) -> None:
        self._name = name
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def invoke(self, request: AgentRequest, run_context: HarnessRunContext) -> AgentResult:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return AgentResult(
            agent=self._name,
            generation_id=run_context.generation_id,
            run_id=run_context.run_id,
        )

    async def stream(
        self, request: AgentRequest, run_context: HarnessRunContext
    ) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(
            agent=self._name,
            generation_id=run_context.generation_id,
            run_id=run_context.run_id,
            kind="end",
        )


async def test_hot_replacing_a_runtime_keeps_the_old_run_working() -> None:
    def runtime_owner(runtime: BlockingRuntime, owner: str):  # type: ignore[no-untyped-def]
        @plugin(name=owner, version="1.0.0")
        async def provide(ctx: PluginContext) -> None:
            ctx.agents.register(runtime, replace=True)

        return provide

    harness = Harness()
    old = BlockingRuntime("worker")
    new = BlockingRuntime("worker")
    harness.install(runtime_owner(old, "owner-old"), entry_id="owner-old")
    await harness.start()

    run = asyncio.create_task(harness.agents.invoke("worker", {"messages": []}))
    await old.entered.wait()

    harness.install(runtime_owner(new, "owner-new"), entry_id="owner-new")
    await harness.reconcile()
    harness.uninstall("owner-old")
    await harness.reconcile()

    old.release.set()
    result = await run

    # The leased run finished on the runtime it started with, and disposing the
    # old owner must not remove its successor.
    assert old.calls == 1
    assert new.calls == 0
    assert result.agent == "worker"
    assert harness.agents.get("worker") is new

    await harness.stop()
