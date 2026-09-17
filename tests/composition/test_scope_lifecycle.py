"""Scoped ownership: setup, rollback, removal, retention, disposal."""

from __future__ import annotations

import asyncio

import pytest
from tests.composition.support import (
    consumer,
    failing,
    generation_of,
    mounted,
    scope_of,
    tracked_provider,
)

from chassis import Harness
from chassis.core.errors import EffectCleanupError, PluginSetupError
from chassis.plugins.lifecycle import PluginState


async def test_scoped_provider_is_owned_by_its_scope() -> None:
    harness = Harness()
    research = harness.composition.child("research")
    research.install(tracked_provider("search", "search"), entry_id="search")
    try:
        await harness.start()
        instance = mounted(harness, "search")

        # The registration is owned by the plugin instance scope, and the resolved
        # tree attributes it to the composition scope that declares the entry.
        registration = harness.capability_registry.by_name("search")[0]
        assert registration.scope_id == instance.scope.id
        assert scope_of(harness, "/research").instances == (instance.instance_id,)
        assert scope_of(harness, "/research").providers == {"search": (instance.instance_id,)}
        assert scope_of(harness, "/research").entries == ("search",)
    finally:
        await harness.stop()


async def test_failed_scoped_setup_rolls_back_its_registrations() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db")
    research = harness.composition.child("research")
    research.install(failing("broken"), entry_id="broken")
    try:
        with pytest.raises(PluginSetupError):
            await harness.start()

        # Nothing was published, and the failed setup left no registration behind.
        assert harness.current_generation is None
        assert harness.capability_registry.by_name("boom") == ()
        assert mounted(harness, "broken").state is PluginState.FAILED
    finally:
        await harness.stop()


async def test_a_failed_candidate_mount_rolls_back_the_whole_scope() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db")
    try:
        await harness.start()
        first = generation_of(harness)

        research = harness.composition.child("research")
        research.install(tracked_provider("search", "search"), entry_id="search")
        # `broken` activates after `search`, so `search` is mounted and must be
        # reverted when the candidate fails.
        research.install(failing("broken"), entry_id="broken")

        with pytest.raises(PluginSetupError):
            await harness.reconcile()

        assert harness.current_generation is first
        assert harness.capability_registry.by_name("boom") == ()
        assert harness.plugin_registry.instance("search") is None
        assert harness.plugin_registry.instance("broken") is not None  # FAILED, not mounted
        assert mounted(harness, "db").generation_refs == 1
    finally:
        await harness.stop()


async def test_replacing_a_provider_inside_one_scope_leaves_others_untouched() -> None:
    """A change in one scope must not disturb the composition of its siblings."""

    disposals: list[str] = []
    harness = Harness()
    harness.install(tracked_provider("shared-db", "database", disposals=disposals), entry_id="db")
    research = harness.composition.child("research")
    research.install(
        tracked_provider("search-v1", "search", disposals=disposals), entry_id="search"
    )
    finance = harness.composition.child("finance")
    finance.install(tracked_provider("erp", "erp", disposals=disposals), entry_id="erp")
    release = asyncio.Event()
    entered = asyncio.Event()
    task: asyncio.Task[str] | None = None
    try:
        await harness.start()
        first = generation_of(harness)
        shared = mounted(harness, "db")
        finance_instance = mounted(harness, "erp")
        search_v1 = mounted(harness, "search")

        # A run keeps the first generation alive, which is what makes the old
        # scope's resources observable after the change.
        async def run() -> str:
            async with harness.acquire() as generation:
                entered.set()
                await release.wait()
                return generation.generation_id

        task = asyncio.create_task(run())
        await entered.wait()

        research.install(
            tracked_provider("search-v2", "search", disposals=disposals),
            entry_id="search",
            replace=True,
        )
        await harness.reconcile()

        assert generation_of(harness).generation_id != first.generation_id
        # Unaffected scopes keep their instances: only the replaced one is new.
        assert mounted(harness, "db") is shared
        assert mounted(harness, "erp") is finance_instance
        assert mounted(harness, "search") is not search_v1
        assert shared.generation_refs == 2
        assert finance_instance.generation_refs == 2

        # The old generation still reaches search-v1, so it is not disposed yet.
        assert search_v1.state is PluginState.ACTIVE
        assert disposals == []

        diff = harness.diagnostics.diff_generations(
            first.generation_id, generation_of(harness).generation_id
        )
        assert [item.subject for item in diff.providers] == ["search"]
        assert diff.providers[0].kind == "replaced"
        assert diff.scopes == ()

        release.set()
        assert await task == first.generation_id

        assert search_v1.state is PluginState.DISPOSED
        # Only the superseded instance was disposed; the current one is alive.
        assert disposals == ["search-v1"]
        assert mounted(harness, "search").manifest.name == "search-v2"
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await harness.stop()


async def test_removing_a_scope_keeps_it_visible_to_old_generations() -> None:
    disposals: list[str] = []
    harness = Harness()
    harness.install(tracked_provider("db", "database", disposals=disposals), entry_id="db")
    research = harness.composition.child("research")
    research.install(tracked_provider("search", "search", disposals=disposals), entry_id="search")
    release = asyncio.Event()
    entered = asyncio.Event()
    task: asyncio.Task[str] | None = None
    try:
        await harness.start()
        first = generation_of(harness)
        search_instance = mounted(harness, "search")

        async def run() -> str:
            async with harness.acquire() as generation:
                entered.set()
                await release.wait()
                return generation.generation_id

        task = asyncio.create_task(run())
        await entered.wait()

        removed = harness.composition.remove("/research")
        result = await harness.reconcile()

        assert removed == ("search",)
        assert result.disposed == ()
        assert "/research" not in generation_of(harness).scopes.paths()
        # The old run still observes the scope it acquired, and its resources.
        assert first.scopes.paths() == ("/", "/research")
        assert search_instance.state is PluginState.ACTIVE
        assert search_instance.generation_refs == 1

        release.set()
        assert await task == first.generation_id

        # Releasing the last lease makes the removed scope's resources unreachable.
        assert search_instance.state is PluginState.DISPOSED
        assert disposals == ["search"]
        assert harness.capability_registry.by_name("search") == ()
        assert mounted(harness, "db").state is PluginState.ACTIVE
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await harness.stop()


async def test_disposal_waits_for_the_last_lease_of_a_scoped_plugin() -> None:
    disposals: list[str] = []
    harness = Harness()
    research = harness.composition.child("research")
    research.install(tracked_provider("search", "search", disposals=disposals), entry_id="search")
    try:
        await harness.start()
        instance = mounted(harness, "search")

        entered = asyncio.Event()
        release = asyncio.Event()

        async def run() -> None:
            async with harness.acquire():
                entered.set()
                await release.wait()

        task = asyncio.create_task(run())
        await entered.wait()

        harness.uninstall("search")
        await harness.reconcile()

        assert disposals == []
        assert instance.state is PluginState.ACTIVE

        release.set()
        await task

        assert disposals == ["search"]
        assert instance.state is PluginState.DISPOSED
    finally:
        await harness.stop()


async def test_shutdown_disposes_scoped_resources() -> None:
    disposals: list[str] = []
    harness = Harness()
    research = harness.composition.child("research")
    research.install(tracked_provider("search", "search", disposals=disposals), entry_id="search")
    research.install(consumer("agent", requires={"search": ">=1,<2"}), entry_id="agent")

    await harness.start()
    await harness.stop()

    assert sorted(disposals) == ["search"]
    assert harness.plugin_registry.instances() == ()


async def test_scoped_setup_failure_reports_cleanup_failures() -> None:
    # A scope whose teardown fails still reports the failure rather than silently
    # leaking the effect, using the same aggregation as a flat composition.
    from chassis import PluginContext, plugin

    @plugin(name="noisy", version="1.0.0")
    async def noisy(ctx: PluginContext) -> None:
        def broken() -> None:
            raise RuntimeError("cleanup failed")

        ctx.cleanup("broken cleanup", broken)

    harness = Harness()
    harness.composition.child("research").install(noisy, entry_id="noisy")
    await harness.start()

    with pytest.raises(EffectCleanupError) as excinfo:
        await harness.stop()

    assert len(excinfo.value.failures) == 1
