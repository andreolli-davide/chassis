"""A reconcile with no semantic change must not publish a new generation.

Scope provenance records which provider satisfied a requirement. It must name the
provider *entry* semantically: the resolver computes a plan before anything is
mounting, so its cached instance ids are not part of what a published generation
compares on.
"""

from __future__ import annotations

from tests.composition.support import consumer, generation_of, tracked_provider

from chassis import Harness


async def test_reconciling_an_unchanged_composition_reuses_the_generation() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db")
    harness.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    try:
        await harness.start()
        first = generation_of(harness)

        result = await harness.reconcile()

        assert generation_of(harness) is first
        assert set(result.reused) == {"db", "agent"}
        assert result.mounted == ()
    finally:
        await harness.stop()
