"""Rollback must not disturb nodes a live generation already owns.

An incremental candidate reuses nodes from older generations. If building a new
node fails, only candidate-owned resources are rolled back; reused resources stay
untouched and the current generation remains current.
"""

from __future__ import annotations

import pytest
from tests.composition.support import failing, generation_of, mounted, tracked_provider

from chassis import Harness
from chassis.core.errors import PluginSetupError
from chassis.plugins.lifecycle import PluginState


async def test_a_failed_candidate_leaves_reused_nodes_untouched() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db")
    try:
        await harness.start()
        current = generation_of(harness)
        db = mounted(harness, "db")

        async with harness.acquire():
            harness.install(failing("broken"), entry_id="broken")
            with pytest.raises(PluginSetupError):
                await harness.reconcile()

            # No partial candidate became visible; the reused provider is intact
            # and still reachable from the generation that owns it.
            assert harness.current_generation is current
            assert mounted(harness, "db") is db
            assert db.state is PluginState.ACTIVE
            assert db.generation_refs == 1
    finally:
        await harness.stop()


async def test_a_failure_after_a_new_node_is_built_rolls_back_only_that_node() -> None:
    harness = Harness()
    disposals: list[str] = []
    harness.install(tracked_provider("db", "database"), entry_id="db")
    try:
        await harness.start()
        current = generation_of(harness)
        db = mounted(harness, "db")

        async with harness.acquire():
            harness.install(tracked_provider("mem", "memory", disposals=disposals), entry_id="mem")
            harness.install(failing("zzz-broken"), entry_id="zzz-broken")
            with pytest.raises(PluginSetupError):
                await harness.reconcile()

            assert harness.current_generation is current
            assert harness.plugin_registry.instance("mem") is None
            assert disposals == ["mem"]
            assert mounted(harness, "db") is db
            assert db.state is PluginState.ACTIVE
    finally:
        await harness.stop()
