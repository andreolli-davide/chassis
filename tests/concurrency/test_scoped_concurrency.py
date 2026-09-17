"""Concurrency: scoped composition must stay pinned for in-flight runs.

Each test drives the real harness while the control plane publishes generations
around a live run, then asserts that the scope tree a run acquired is the tree it
keeps observing and that scoped resources are disposed only when unreachable.
"""

from __future__ import annotations

import asyncio

from tests.composition.support import generation_of, mounted, scope_of

from chassis import DATABASE, Harness, PluginContext, plugin
from chassis.capabilities import CapabilityKey, ScopedCapabilities
from chassis.plugins.lifecycle import PluginState


class Guard:
    """Provider payload that fails loudly if it is used after disposal."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.disposed = False

    def use(self) -> str:
        if self.disposed:
            raise AssertionError(f"{self.name} was used after dispose")
        return self.name


def guard_plugin(name: str, capability: CapabilityKey, disposals: list[str]):  # type: ignore[no-untyped-def]
    @plugin(name=name, version="1.0.0", provides={capability.name: "1.0.0"})
    async def guard(ctx: PluginContext) -> None:
        value = Guard(name)
        capabilities: ScopedCapabilities = ctx.capabilities
        capabilities.provide(capability, value)

        def on_close() -> None:
            value.disposed = True
            disposals.append(name)

        ctx.cleanup(f"{name} disposed", on_close)

    return guard


async def test_a_run_keeps_the_scope_tree_it_acquired() -> None:
    harness = Harness()
    harness.install(guard_plugin("root-db", DATABASE, []), entry_id="root-db")
    research = harness.composition.child("research")
    research.install(guard_plugin("search", CapabilityKey("search", "1"), []), entry_id="search")
    try:
        await harness.start()

        entered = asyncio.Event()
        release = asyncio.Event()
        observed: list[tuple[str, ...]] = []

        async def run() -> None:
            async with harness.acquire() as generation:
                before = generation.scopes.paths()
                entered.set()
                await release.wait()
                after = generation.scopes.paths()
                research_scope = generation.scopes.get("/research")
                assert research_scope is not None
                assert research_scope.entries == ("search",)
                observed.append((*before, *after))

        task = asyncio.create_task(run())
        await entered.wait()

        # The control plane removes the scope and publishes while the run is live.
        harness.composition.remove("/research")
        await harness.reconcile()

        assert generation_of(harness).scopes.paths() == ("/",)
        assert generation_of(harness).scopes.get("/research") is None

        release.set()
        await task

        # The run observed the old tree before and after the publication, unchanged.
        assert observed == [("/", "/research", "/", "/research")]
    finally:
        await harness.stop()


async def test_new_runs_see_the_new_scope_tree() -> None:
    harness = Harness()
    harness.install(guard_plugin("root-db", DATABASE, []), entry_id="root-db")
    try:
        await harness.start()
        first = generation_of(harness)

        entered = asyncio.Event()
        release = asyncio.Event()

        async def run() -> None:
            async with harness.acquire():
                entered.set()
                await release.wait()

        task = asyncio.create_task(run())
        await entered.wait()

        harness.composition.child("research").install(
            guard_plugin("search", CapabilityKey("search", "1"), []), entry_id="search"
        )
        await harness.reconcile()
        second = generation_of(harness)
        assert second.generation_id != first.generation_id

        async with harness.acquire() as generation:
            assert generation is second
            assert generation.scopes.paths() == ("/", "/research")

        release.set()
        await task
    finally:
        await harness.stop()


async def test_scoped_resources_are_not_disposed_while_an_old_generation_is_leased() -> None:
    disposals: list[str] = []
    harness = Harness()
    research = harness.composition.child("research")
    research.install(
        guard_plugin("search", CapabilityKey("search", "1"), disposals), entry_id="search"
    )
    try:
        await harness.start()
        instance = mounted(harness, "search")

        entered = [asyncio.Event() for _ in range(4)]
        release = asyncio.Event()

        async def run(index: int) -> None:
            async with harness.acquire():
                entered[index].set()
                await release.wait()

        runners = [asyncio.create_task(run(index)) for index in range(len(entered))]
        await asyncio.gather(*(event.wait() for event in entered))

        harness.composition.remove("/research")
        await harness.reconcile()

        # Four live leases on the old generation keep the removed scope's plugin.
        assert disposals == []
        assert instance.state is PluginState.ACTIVE
        assert instance.generation_refs == 1

        release.set()
        await asyncio.gather(*runners)

        assert disposals == ["search"]
        assert instance.state is PluginState.DISPOSED
    finally:
        await harness.stop()


async def test_diagnostics_while_generations_coexist() -> None:
    disposals: list[str] = []
    harness = Harness()
    harness.install(guard_plugin("root-db", DATABASE, disposals), entry_id="root-db")
    try:
        await harness.start()
        first = generation_of(harness)

        entered = asyncio.Event()
        release = asyncio.Event()

        async def run() -> None:
            async with harness.acquire():
                entered.set()
                await release.wait()

        task = asyncio.create_task(run())
        await entered.wait()

        harness.composition.child("research").install(
            guard_plugin("search", CapabilityKey("search", "1"), disposals), entry_id="search"
        )
        await harness.reconcile()
        second = generation_of(harness)

        live = harness.generation_manager.live()
        assert first in live and second in live

        # Diagnostics never confuse the two generations.
        assert [scope.path for scope in first.scopes] == ["/"]
        assert (
            harness.diagnostics.explain_scope("/", generation_id=first.generation_id).children == ()
        )
        assert harness.diagnostics.explain_scope(
            "/", generation_id=second.generation_id
        ).children == ("/research",)
        assert harness.diagnostics.explain_scope("/research").generation_id == second.generation_id

        diff = harness.diagnostics.diff_generations(first.generation_id, second.generation_id)
        assert {item.subject for item in diff.scopes if item.kind == "added"} == {"/research"}
        assert harness.diagnostics.explain_requirement("search", "database") is None

        release.set()
        await task

        # The old generation is gone; diagnostics still answer for the new one.
        assert scope_of(harness, "/research").entries == ("search",)
    finally:
        await harness.stop()


async def test_concurrent_reconciles_with_scopes_are_serialized() -> None:
    harness = Harness()
    harness.install(guard_plugin("root-db", DATABASE, []), entry_id="root-db")
    research = harness.composition.child("research")
    try:
        await harness.start()

        for revision in range(1, 5):
            research.install(
                guard_plugin(f"search-{revision}", CapabilityKey("search", "1"), []),
                entry_id="search",
                replace=True,
            )
            await asyncio.gather(
                harness.reconcile(),
                harness.reconcile(),
                harness.reconcile(),
            )

        assert len(harness.plugin_registry.instances()) == 2
        assert mounted(harness, "search").manifest.name == "search-4"
        assert scope_of(harness, "/research").entries == ("search",)
        assert harness.generation_manager.draining() == ()
    finally:
        await harness.stop()
