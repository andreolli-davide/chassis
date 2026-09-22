"""Preview mutates nothing: every authoritative state object is untouched.

The zero-mutation proof (guarantee G26) snapshots the authoritative state a
reconcile would mutate — desired entries and revisions, mounted instances, the
dirty flag, generations, capability/tool/hook/agent registrations, drift, and
the applied configuration — before and after `Harness.preview()`, both for the
current state and for a declarative configuration, and requires exact equality.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.planning.support import db_provider, memory_consumer

from chassis import ConfigurationError, Harness
from chassis.testing import TestHarness

CONFIG = {
    "version": 1,
    "plugins": [
        {"id": "orders-db", "plugin": "orders-db"},
        {"id": "memory", "plugin": "memory"},
    ],
}

REPLACEMENT_CONFIG = {
    "version": 1,
    "plugins": [
        {"id": "orders-db", "plugin": "orders-db", "config": {"pool": 2}},
        {"id": "memory", "plugin": "memory"},
    ],
}


def state_of(harness: Harness) -> dict[str, Any]:
    """Everything an apply mutates, as comparable plain data."""

    registry = harness.plugin_registry
    return {
        "entries": [(entry.entry_id, entry.revision) for entry in registry.entries()],
        "instances": [instance.instance_id for instance in registry.instances()],
        "pending": harness.has_pending_changes,
        "generations": harness.diagnostics.generations(),
        "resources": harness.diagnostics.resource_counts().to_dict(),
        "capabilities": harness.diagnostics.capabilities(),
        "tools": harness.diagnostics.tools(),
        "hooks": harness.diagnostics.hooks(),
        "agents": harness.diagnostics.agents(),
        "desired": harness.diagnostics.desired_state(),
        "config": harness.diagnostics.config(),
        "status": harness.diagnostics.status(),
    }


def catalogued(harness: Harness) -> Harness:
    harness.register_plugin_type("orders-db", db_provider)
    harness.register_plugin_type("memory", memory_consumer)
    return harness


async def test_preview_leaves_every_authoritative_state_object_unchanged() -> None:
    harness = catalogued(Harness())
    harness.install(db_provider, entry_id="orders-db")

    before = state_of(harness)
    harness.preview()
    harness.preview(REPLACEMENT_CONFIG)
    assert state_of(harness) == before

    await harness.start()
    try:
        before = state_of(harness)
        harness.preview()
        harness.preview(REPLACEMENT_CONFIG)
        assert state_of(harness) == before
    finally:
        await harness.stop()


async def test_preview_never_mounts_revises_or_dirties() -> None:
    harness = catalogued(TestHarness())
    harness.install(db_provider, entry_id="orders-db")
    await harness.start()
    try:
        entry = harness.plugin_registry.entry("orders-db")
        assert entry is not None
        revision = entry.revision

        harness.preview(REPLACEMENT_CONFIG)

        refreshed = harness.plugin_registry.entry("orders-db")
        assert refreshed is not None
        assert refreshed.revision == revision, "preview bumped an entry revision"
        assert harness.has_pending_changes is False, "preview set the dirty flag"
        assert harness.diagnostics.resource_counts().instances == len(
            harness.plugin_registry.instances()
        )
    finally:
        await harness.stop()


async def test_a_rejected_preview_configuration_also_mutates_nothing() -> None:
    harness = catalogued(Harness())
    harness.install(db_provider, entry_id="orders-db")
    before = state_of(harness)

    with pytest.raises(ConfigurationError):
        harness.preview({"plugins": [{"id": "x", "plugin": "never-registered"}]})
    with pytest.raises(ConfigurationError):
        harness.preview({"plugins": [], "provider_preferences": {"": ""}})

    assert state_of(harness) == before
