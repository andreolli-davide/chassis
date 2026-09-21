from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from chassis import Harness, Plugin, PluginContext, PluginManifest, PluginSetupError, plugin
from chassis.capabilities import CapabilityKey
from chassis.core.errors import EffectCleanupError, PluginCycleError
from chassis.plugins.lifecycle import PluginInstance, PluginState

DATABASE = CapabilityKey("database", "1")
MEMORY = CapabilityKey("memory", "1")
TOOLS = CapabilityKey("tools", "1")


class Marker:
    """Opaque provider payload used to prove identity hand-off."""

    def __init__(self, name: str) -> None:
        self.name = name


def mounted(harness: Harness, entry_id: str) -> PluginInstance:
    instance = harness.plugin_registry.instance(entry_id)
    assert instance is not None, f"entry {entry_id!r} is not mounted"
    return instance


def database_plugin(*, version: str = "1.0.0", on_close: Callable[[], None] | None = None):  # type: ignore[no-untyped-def]
    @plugin(name="postgres", version=version, provides={"database": version})
    async def postgres(ctx: PluginContext) -> None:
        ctx.capabilities.provide(DATABASE, Marker("postgres"), version=version)
        if on_close is not None:
            ctx.cleanup("postgres closed", on_close)

    return postgres


def memory_plugin(*, version: str = "1.0.0", requires: str = ">=1,<2"):  # type: ignore[no-untyped-def]
    @plugin(
        name="memory",
        version=version,
        provides={"memory": version},
        requires={"database": requires},
    )
    async def memory(ctx: PluginContext) -> None:
        database = ctx.require(DATABASE)
        assert isinstance(database, Marker)
        ctx.capabilities.provide(MEMORY, Marker(f"memory-on-{database.name}"), version=version)

    return memory


def agent_extension_plugin():  # type: ignore[no-untyped-def]
    @plugin(name="agent-extension", version="1.0.0", requires={"memory": ">=1,<2"})
    async def agent_extension(ctx: PluginContext) -> None:
        memory = ctx.require(MEMORY)
        assert isinstance(memory, Marker)
        ctx.capabilities.provide(TOOLS, Marker(f"tools-on-{memory.name}"), version="1.0.0")

    return agent_extension


async def test_mount_activates_plugin_and_registers_capability() -> None:
    harness = Harness()
    harness.install(database_plugin(), entry_id="db")

    await harness.start()
    try:
        instance = mounted(harness, "db")
        assert instance.state is PluginState.ACTIVE
        assert instance.health.value == "healthy"
        assert [
            registration.key for registration in harness.capability_registry.registrations()
        ] == [DATABASE]
        assert instance.scope.parent is None
    finally:
        await harness.stop()


async def test_installation_order_does_not_define_dependency_semantics() -> None:
    harness = Harness()
    harness.install(memory_plugin(), entry_id="memory")
    harness.install(database_plugin(), entry_id="db")

    result = await harness.start()
    try:
        assert result.plan.activation_order == ("db", "memory")
        assert mounted(harness, "memory").state is PluginState.ACTIVE
    finally:
        await harness.stop()


async def test_removing_provider_cascades_and_restoring_reactivates() -> None:
    harness = Harness()
    await harness.start()
    try:
        harness.install(database_plugin(), entry_id="db")
        harness.install(memory_plugin(), entry_id="memory")
        harness.install(agent_extension_plugin(), entry_id="agent")
        result = await harness.reconcile()
        assert result.plan.activation_order == ("db", "memory", "agent")

        # Removing the provider must remove its dependents too, and must not
        # dispose the provider before the consumers that still reach it.
        harness.uninstall("db")
        cascade = await harness.reconcile()

        assert cascade.plan.pending == ("agent", "memory")
        assert set(cascade.disposed) == {"db", "memory", "agent"}
        assert cascade.disposed.index("agent") < cascade.disposed.index("memory")
        assert cascade.disposed.index("memory") < cascade.disposed.index("db")
        assert harness.capability_registry.registrations() == ()
        assert harness.plugin_registry.instances() == ()

        # Restoring the provider reactivates everything that depended on it.
        harness.install(database_plugin(), entry_id="db")
        restored = await harness.reconcile()

        assert restored.plan.activation_order == ("db", "memory", "agent")
        assert restored.mounted == ("db", "memory", "agent")
        assert {
            registration.key for registration in harness.capability_registry.registrations()
        } == {
            DATABASE,
            MEMORY,
            TOOLS,
        }
    finally:
        await harness.stop()


async def test_pending_plugin_activates_when_the_provider_is_installed_later() -> None:
    harness = Harness()
    harness.install(memory_plugin(), entry_id="memory")
    await harness.start()
    try:
        assert harness.plugin_registry.instance("memory") is None

        harness.install(database_plugin(), entry_id="db")
        result = await harness.reconcile()

        assert result.mounted == ("db", "memory")
        assert mounted(harness, "memory").state is PluginState.ACTIVE
    finally:
        await harness.stop()


async def test_setup_failure_rolls_back_every_effect() -> None:
    cleaned: list[str] = []

    @plugin(name="broken", version="1.0.0", provides={"database": "1.0.0"})
    async def broken(ctx: PluginContext) -> None:
        ctx.capabilities.provide(DATABASE, Marker("broken"))
        ctx.cleanup("broken resource", lambda: cleaned.append("resource"))
        ctx.create_task(asyncio.sleep(30), name="broken-worker")
        raise RuntimeError("setup exploded")

    harness = Harness()
    harness.install(broken, entry_id="broken")

    with pytest.raises(PluginSetupError) as excinfo:
        await harness.reconcile()

    assert excinfo.value.context["plugin"] == "broken"
    assert isinstance(excinfo.value.__cause__, RuntimeError)

    instance = mounted(harness, "broken")
    assert instance.state is PluginState.FAILED
    assert instance.scope.is_closed
    assert instance.scope.tasks == ()
    assert harness.capability_registry.registrations() == ()
    assert cleaned == ["resource"]

    await harness.stop()


async def test_failed_candidate_mount_rolls_back_the_whole_candidate() -> None:
    @plugin(name="broken", version="1.0.0", requires={"database": ">=1,<2"})
    async def broken(ctx: PluginContext) -> None:
        ctx.require(DATABASE)
        raise RuntimeError("setup exploded")

    harness = Harness()
    harness.install(database_plugin(), entry_id="db")
    harness.install(broken, entry_id="broken")

    with pytest.raises(PluginSetupError):
        await harness.reconcile()

    # A failed candidate must not become visible: the database mounted for the
    # same candidate is rolled back, leaving only the failed instance behind.
    assert harness.plugin_registry.instance("db") is None
    assert mounted(harness, "broken").state is PluginState.FAILED
    assert harness.capability_registry.registrations() == ()

    # The next reconciliation can still build a valid subset.
    harness.uninstall("broken")
    result = await harness.reconcile()
    assert result.plan.activation_order == ("db",)
    assert mounted(harness, "db").state is PluginState.ACTIVE

    await harness.stop()


async def test_reconcile_rolls_back_failed_candidate_and_keeps_composition() -> None:
    @plugin(name="failing", version="1.0.0", requires={"database": ">=1,<2"})
    async def failing(ctx: PluginContext) -> None:
        ctx.require(DATABASE)
        raise RuntimeError("nope")

    harness = Harness()
    harness.install(database_plugin(), entry_id="db")
    await harness.start()
    try:
        harness.install(failing, entry_id="failing")
        with pytest.raises(PluginSetupError):
            await harness.reconcile()

        assert mounted(harness, "db").state is PluginState.ACTIVE
        assert harness.capability_registry.registrations() != ()
    finally:
        harness.uninstall("failing")
        await harness.stop()


async def test_cleanup_failure_is_aggregated_and_other_plugins_still_dispose() -> None:
    def exploding_cleanup() -> None:
        raise RuntimeError("cleanup exploded")

    harness = Harness()
    harness.install(database_plugin(on_close=exploding_cleanup), entry_id="db")
    harness.install(memory_plugin(), entry_id="memory")
    await harness.start()

    memory = mounted(harness, "memory")
    database = mounted(harness, "db")

    with pytest.raises(EffectCleanupError) as excinfo:
        await harness.stop()

    assert [failure.description for failure in excinfo.value.failures] == ["postgres closed"]
    assert memory.state is PluginState.DISPOSED
    assert database.state is PluginState.DISPOSED
    assert harness.plugin_registry.instances() == ()


async def test_shutdown_is_idempotent() -> None:
    harness = Harness()
    harness.install(database_plugin(), entry_id="db")
    await harness.start()

    await harness.stop()
    await harness.stop()

    assert harness.state.value == "stopped"


async def test_dependency_cycle_is_detected_and_reported() -> None:
    @plugin(
        name="a",
        version="1.0.0",
        provides={"tools": "1.0.0"},
        requires={"memory": ">=1,<2"},
    )
    async def plugin_a(ctx: PluginContext) -> None:
        ctx.capabilities.provide(TOOLS, Marker("a"))

    @plugin(
        name="b",
        version="1.0.0",
        provides={"memory": "1.0.0"},
        requires={"tools": ">=1,<2"},
    )
    async def plugin_b(ctx: PluginContext) -> None:
        ctx.capabilities.provide(MEMORY, Marker("b"))

    harness = Harness()
    harness.install(plugin_a, entry_id="a")
    harness.install(plugin_b, entry_id="b")

    with pytest.raises(PluginCycleError) as excinfo:
        await harness.reconcile()

    assert excinfo.value.context["cycles"] == ["a -> b -> a"]
    assert harness.plan().pending == ("a", "b")

    await harness.stop()


async def test_capability_version_mismatch_is_diagnosed() -> None:
    harness = Harness()
    harness.install(database_plugin(version="2.0.0"), entry_id="db")
    harness.install(memory_plugin(requires=">=1,<2"), entry_id="memory")

    plan = harness.plan()

    assert plan.activation_order == ("db",)
    assert plan.pending == ("memory",)

    memory_plan = plan.plan_for("memory")
    assert memory_plan is not None
    resolution = memory_plan.requirements[0]
    assert resolution.status == "version_mismatch"
    assert "registered: db@2.0.0" in resolution.explain
    assert "version_mismatch" in harness.diagnostics.explain("memory")

    await harness.stop()


async def test_self_provided_capability_does_not_satisfy_the_requirement() -> None:
    @plugin(
        name="selfish",
        version="1.0.0",
        provides={"tools": "1.0.0"},
        requires={"tools": ">=1,<2"},
    )
    async def selfish(ctx: PluginContext) -> None:
        ctx.capabilities.provide(TOOLS, Marker("selfish"))

    harness = Harness()
    harness.install(selfish, entry_id="selfish")

    plan = harness.plan()

    assert plan.pending == ("selfish",)
    selfish_plan = plan.plan_for("selfish")
    assert selfish_plan is not None
    assert selfish_plan.requirements[0].status == "self_reference"

    await harness.stop()


async def test_ambiguous_provider_requires_explicit_selection() -> None:
    @plugin(name="consumer", version="1.0.0", requires={"database": ">=1,<2"})
    async def consumer(ctx: PluginContext) -> None:
        ctx.require(DATABASE)

    harness = Harness()
    harness.install(database_plugin(), entry_id="db-a")
    harness.install(database_plugin(), entry_id="db-b")
    harness.install(consumer, entry_id="consumer")

    plan = harness.plan()
    assert plan.pending == ("consumer",)
    consumer_plan = plan.plan_for("consumer")
    assert consumer_plan is not None
    assert consumer_plan.requirements[0].status == "ambiguous"

    harness.prefer_provider("consumer:database", "db-b")
    await harness.start()
    try:
        assert harness.plan().activation_order == ("db-a", "db-b", "consumer")
        selected = mounted(harness, "consumer").resolved["database"]
        assert selected.provider_id == mounted(harness, "db-b").instance_id
    finally:
        await harness.stop()


async def test_plugin_owned_tasks_are_cancelled_on_dispose() -> None:
    cancelled = asyncio.Event()

    @plugin(name="worker", version="1.0.0", provides={"tools": "1.0.0"})
    async def worker(ctx: PluginContext) -> None:
        async def background() -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        ctx.create_task(background(), name="background")
        ctx.capabilities.provide(TOOLS, Marker("worker"))

    harness = Harness()
    harness.install(worker, entry_id="worker")
    await harness.start()

    instance = mounted(harness, "worker")
    assert len(instance.scope.tasks) == 1

    await harness.stop()

    assert cancelled.is_set()
    assert instance.scope.tasks == ()
    assert instance.state is PluginState.DISPOSED


async def test_registrations_do_not_survive_their_owning_scope() -> None:
    harness = Harness()
    harness.install(database_plugin(), entry_id="db")
    await harness.start()

    assert len(harness.capability_registry) == 1
    await harness.stop()

    assert harness.capability_registry.registrations() == ()
    assert harness.capability_registry.get("cap_missing") is None


async def test_teardown_runs_before_scope_unwind() -> None:
    events: list[str] = []

    class OrderedPlugin(Plugin):
        manifest = PluginManifest(name="ordered", version="1.0.0", provides={"tools": "1.0.0"})

        async def setup(self, ctx: PluginContext) -> None:
            ctx.capabilities.provide(TOOLS, Marker("ordered"))
            ctx.cleanup("resource", lambda: events.append("scope"))

        async def teardown(self, ctx: PluginContext) -> None:
            events.append("teardown")

    harness = Harness()
    harness.install(OrderedPlugin(), entry_id="ordered")
    await harness.start()
    await harness.stop()

    assert events == ["teardown", "scope"]


async def test_diagnostics_explain_pending_plugin() -> None:
    harness = Harness()
    harness.install(memory_plugin(), entry_id="memory")
    await harness.start()
    try:
        payload = harness.diagnostics.plugin("memory")
        assert payload is not None
        assert payload["eligible"] is False
        assert payload["state"] is None
        assert payload["requirements"][0]["status"] == "no_provider"
        assert "no provider for 'database'" in payload["reasons"][0]

        status = harness.diagnostics.status()
        assert status["state"] == "running"
        assert status["plugins"]["desired"] == 1
        assert status["composition"]["pending"] == 1
        assert status["capabilities"] == 0
        assert harness.diagnostics.capabilities() == []
        assert harness.diagnostics.dependencies()["pending"] == ["memory"]
    finally:
        await harness.stop()


async def test_decoration_produces_a_reusable_plugin_class() -> None:
    @plugin(name="reusable", version="1.0.0", provides={"tools": "1.0.0"})
    async def reusable(ctx: PluginContext) -> None:
        ctx.capabilities.provide(TOOLS, Marker("reusable"))

    assert issubclass(reusable, Plugin)
    assert reusable.__name__ == "reusable"

    harness = Harness()
    harness.install(reusable, entry_id="first")
    harness.install(reusable, entry_id="second")
    await harness.start()
    try:
        assert mounted(harness, "first").instance_id != mounted(harness, "second").instance_id
        assert len(harness.capability_registry) == 2
    finally:
        await harness.stop()


async def test_failed_setup_aggregates_rollback_cleanup_failures() -> None:
    @plugin(name="bad", version="1.0.0", provides={"database": "1.0.0"})
    async def bad(ctx: PluginContext) -> None:
        ctx.capabilities.provide(DATABASE, Marker("bad"))

        def broken_disposer() -> None:
            raise RuntimeError("disposal failed")

        ctx.cleanup("broken effect", broken_disposer)
        raise RuntimeError("setup failed")

    harness = Harness()
    harness.install(bad, entry_id="bad")
    try:
        with pytest.raises(PluginSetupError) as excinfo:
            await harness.start()

        error = excinfo.value
        assert error.context["error"] == "RuntimeError"
        # The rollback cleanup failure is aggregated into the setup error,
        # not hidden inside the failed scope.
        assert error.context["cleanup_failures"] == 1
        assert [failure.description for failure in error.cleanup_failures] == ["broken effect"]
        assert isinstance(error.__cause__, RuntimeError)
        assert any(
            failure.description == "broken effect" for failure in harness.last_cleanup_failures
        )
    finally:
        await harness.stop()


async def test_orphaned_failed_instances_are_reclaimed() -> None:
    @plugin(name="broken", version="1.0.0", provides={"database": "1.0.0"})
    async def broken(ctx: PluginContext) -> None:
        raise RuntimeError("setup failed")

    harness = Harness()
    harness.install(broken, entry_id="broken")
    try:
        with pytest.raises(PluginSetupError):
            await harness.start()

        # While the entry is still desired the failed instance stays
        # inspectable for diagnostics and retry.
        instance = mounted(harness, "broken")
        assert instance.state is PluginState.FAILED

        # Once it is no longer desired it is reclaimed, not left resident.
        harness.uninstall("broken")
        await harness.reconcile()
        assert harness.plugin_registry.instance("broken") is None
        assert harness.plugin_registry.instances() == ()
    finally:
        await harness.stop()
