"""A localized change must not rebuild the rest of the composition.

This is the deterministic efficiency measurement for 0.4: counts, not wall-clock
thresholds. With 100 scopes each owning a provider/consumer pair, changing one
provider must rebuild that pair and reuse the other 198 nodes.
"""

from __future__ import annotations

from tests.composition.support import consumer, tracked_provider

from chassis import Harness

SCOPES = 100


async def test_one_scoped_change_rebuilds_only_its_dependency_closure() -> None:
    harness = Harness()
    for index in range(SCOPES):
        scope = harness.composition.child(f"t{index}")
        scope.install(tracked_provider(f"p{index}", "database"), entry_id=f"p{index}")
        scope.install(consumer(f"a{index}", requires={"database": ">=1,<2"}), entry_id=f"a{index}")
    try:
        await harness.start()
        generation = harness.current_generation
        assert generation is not None
        assert len(generation.instances) == SCOPES * 2

        harness.install(
            tracked_provider("p7", "database"),
            entry_id="p7",
            config={"pool": 2},
            replace=True,
            scope="/t7",
        )
        result = await harness.reconcile()

        assert result.impact is not None
        counts = result.impact.counts()
        assert counts.get("rebuilt") == 1
        assert counts.get("rewired") == 1
        assert counts.get("reused") == SCOPES * 2 - 2

        # Resources set up == nodes mounted; everything else was reused.
        assert set(result.mounted) == {"p7", "a7"}
        assert len(result.reused) == SCOPES * 2 - 2
        assert set(result.disposed) == {"p7", "a7"}
    finally:
        await harness.stop()
