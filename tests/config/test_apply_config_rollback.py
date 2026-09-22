"""A failed `apply_config()` restores the exact prior state (R010/R011).

Rollback must not reinstall entries (a reinstall bumps revisions and re-dirties
the harness) and must not resurrect an entry that was never removed. A rejected
configuration leaves the same entries at the same revisions, and the next
reconcile does not rebuild and publish a generation for it.
"""

from __future__ import annotations

import pytest

from chassis import MODEL, PluginContext, plugin
from chassis.core.errors import PluginLoadError
from chassis.testing import TestHarness


@plugin(name="fake-model", version="1.0.0", provides={"model": "1.0.0"})
async def fake_model(ctx: PluginContext) -> None:
    ctx.capabilities.provide(MODEL, "a-model")


class NotAPlugin:
    """Catalog-registered class that fails the plugin contract at install."""

    def __init__(self, config: object = None) -> None:
        pass


async def test_a_failed_apply_restores_every_entry_exactly() -> None:
    harness = TestHarness()
    harness.register_plugin_type("fake-model", fake_model)
    harness.register_plugin_type("broken", NotAPlugin)  # type: ignore[arg-type]
    harness.apply_config(
        {
            "version": 1,
            "plugins": [
                {"id": "a", "plugin": "fake-model", "config": {"m": "1"}},
                {"id": "b", "plugin": "fake-model", "config": {"m": "2"}},
            ],
        }
    )
    await harness.start()
    published = harness.current_generation
    entry_a = harness.entry("a")
    entry_b = harness.entry("b")
    assert entry_a is not None and entry_a.revision == 1
    assert entry_b is not None and entry_b.revision == 1

    with pytest.raises(PluginLoadError):
        harness.apply_config(
            {
                "version": 1,
                "plugins": [
                    {"id": "a", "plugin": "fake-model", "config": {"m": "changed"}},
                    {"id": "b", "plugin": "broken"},
                ],
            }
        )

    # Restored, not reinstalled: the same objects at the same revisions.
    assert harness.entry("a") is entry_a
    assert harness.entry("a").revision == 1  # type: ignore[union-attr]
    assert harness.entry("b") is entry_b
    assert harness.entry("b").revision == 1  # type: ignore[union-attr]
    assert harness.has_pending_changes is False

    # A rejected configuration is not rebuilt and published by the next
    # reconcile: the acquired generation is still the one published before it.
    await harness.ensure_ready()
    assert harness.current_generation is published


async def test_a_failed_apply_never_half_adds_an_entry() -> None:
    harness = TestHarness()
    harness.register_plugin_type("fake-model", fake_model)
    harness.register_plugin_type("broken", NotAPlugin)  # type: ignore[arg-type]

    with pytest.raises(PluginLoadError):
        harness.apply_config(
            {
                "version": 1,
                "plugins": [
                    {"id": "c", "plugin": "fake-model"},
                    {"id": "d", "plugin": "broken"},
                ],
            }
        )

    assert harness.entry("c") is None
    assert harness.entry("d") is None
    assert harness.has_pending_changes is False

    # The revision counter is restored too: a later, valid application starts
    # the entry back at revision 1.
    harness.apply_config({"version": 1, "plugins": [{"id": "c", "plugin": "fake-model"}]})
    assert harness.entry("c").revision == 1  # type: ignore[union-attr]
