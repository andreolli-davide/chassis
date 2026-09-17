"""A published generation must not observe later control-plane changes.

Desired-state configuration is hashed, compared, and observed by runs, so it must
be immutable in both senses: a caller must not be able to change it, and a
published instance must not share a mutable mapping with the control plane.
"""

from __future__ import annotations

import pytest
from tests.composition.support import mounted, tracked_provider

from chassis import Harness
from chassis.plugins.lifecycle import PluginState


async def test_desired_config_is_not_aliased_into_a_published_instance() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db", config={"pool": 1})
    try:
        await harness.start()
        generation = harness.current_generation
        assert generation is not None
        instance = mounted(harness, "db")
        entry = harness.entry("db")
        assert entry is not None
        context = instance.context
        assert context is not None
        digest = harness.snapshot_for(generation).digest()

        # Mutating the desired entry must be impossible, not silently propagated
        # into the running instance the generation already published.
        with pytest.raises(TypeError):
            entry.config["pool"] = 2  # type: ignore[index]
        with pytest.raises(TypeError):
            context.config["pool"] = 2  # type: ignore[index]

        assert dict(instance.config) == {"pool": 1}
        assert dict(context.config) == {"pool": 1}
        assert harness.snapshot_for(generation).digest() == digest
        assert instance.state is PluginState.ACTIVE
    finally:
        await harness.stop()
