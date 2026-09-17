"""Structural sharing: one runtime instance across several generations.

A reused node is the same lifecycle-managed instance, not a copy. It must survive
while any live generation can reach it and be disposed exactly once, after the
last one disappears.
"""

from __future__ import annotations

from tests.composition.support import consumer, generation_of, mounted, tracked_provider

from chassis import Harness
from chassis.plugins.lifecycle import PluginState


async def test_one_resource_is_shared_across_two_generations() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db")
    try:
        await harness.start()
        first = generation_of(harness)
        shared = mounted(harness, "db")
        async with harness.acquire():
            harness.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
            await harness.reconcile()
            second = generation_of(harness)

            assert mounted(harness, "db") is shared
            assert shared.generation_refs == 2
            assert shared.state is PluginState.ACTIVE
            assert harness.diagnostics.instance_generations(shared.instance_id) == (
                second.generation_id,
                first.generation_id,
            )
    finally:
        await harness.stop()


async def test_one_resource_is_shared_across_three_generations() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db")
    try:
        await harness.start()
        shared = mounted(harness, "db")
        async with harness.acquire():  # lease gen1
            harness.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
            await harness.reconcile()
            async with harness.acquire():  # lease gen2
                harness.install(tracked_provider("telemetry", "telemetry"), entry_id="telemetry")
                await harness.reconcile()

                assert mounted(harness, "db") is shared
                assert shared.generation_refs == 3
                assert len(harness.diagnostics.instance_generations(shared.instance_id)) == 3
    finally:
        await harness.stop()


async def test_a_new_node_is_built_while_an_old_node_is_reused() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db")
    harness.install(tracked_provider("telemetry", "telemetry"), entry_id="telemetry")
    try:
        await harness.start()
        db_before = mounted(harness, "db")
        telemetry_before = mounted(harness, "telemetry")
        async with harness.acquire():
            harness.install(tracked_provider("db2", "database"), entry_id="db", replace=True)
            harness.install(tracked_provider("mem", "memory"), entry_id="mem")
            result = await harness.reconcile()

            # Unrelated nodes reused; the replaced provider rebuilt; the new one mounted.
            assert mounted(harness, "db") is not db_before
            assert mounted(harness, "telemetry") is telemetry_before
            assert telemetry_before.generation_refs == 2
            assert set(result.reused) == {"telemetry"}
            assert set(result.mounted) == {"db", "mem"}
    finally:
        await harness.stop()


async def test_a_removed_node_is_retained_by_an_older_generation() -> None:
    harness = Harness()
    disposals: list[str] = []
    harness.install(tracked_provider("db", "database", disposals=disposals), entry_id="db")
    try:
        await harness.start()
        instance = mounted(harness, "db")
        async with harness.acquire():
            harness.uninstall("db")
            result = await harness.reconcile()

            assert result.disposed == ()
            assert instance.state is PluginState.ACTIVE
            assert instance.generation_refs == 1
            assert disposals == []

        assert instance.state is PluginState.DISPOSED
        assert disposals == ["db"]
        assert harness.diagnostics.instance_generations(instance.instance_id) == ()
    finally:
        await harness.stop()


async def test_a_resource_is_disposed_only_after_its_last_generation_disappears() -> None:
    harness = Harness()
    disposals: list[str] = []
    harness.install(tracked_provider("db", "database", disposals=disposals), entry_id="db")
    try:
        await harness.start()
        instance = mounted(harness, "db")

        # The older, leased generation still reaches the resource after it leaves
        # the current composition, so retirement and disposal wait.
        async with harness.acquire():
            harness.uninstall("db")
            await harness.reconcile()
            assert instance.state is PluginState.ACTIVE
            assert instance.generation_refs == 1
            assert disposals == []

        assert instance.state is PluginState.DISPOSED
        assert disposals == ["db"]
    finally:
        await harness.stop()
