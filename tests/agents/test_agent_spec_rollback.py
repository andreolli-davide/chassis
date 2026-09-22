"""A failed AgentSpec materialization restores the exact prior state (R010).

The same capture-then-restore discipline as `apply_config()`: rollback must not
reinstall entries (a reinstall bumps revisions and re-dirties the harness), and
a later, valid materialization starts its entries back at revision 1.
"""

from __future__ import annotations

import pytest

from chassis.agent_spec import AgentSpec
from chassis.capabilities import DATABASE
from chassis.core.errors import PluginLoadError
from chassis.plugins import PluginContext, plugin
from chassis.testing import TestHarness


@plugin(name="worker", version="1.0.0", provides={"database": "1.0.0"})
async def worker(ctx: PluginContext) -> None:
    ctx.capabilities.provide(DATABASE, "worker-db")


class NotAPlugin:
    """Plugin contribution that fails the plugin contract at install."""

    def __init__(self, config: object = None) -> None:
        pass


async def test_a_failed_materialization_restores_the_prior_entries() -> None:
    harness = TestHarness()
    harness.agents.install(AgentSpec(name="demo", revision="1", plugins={"worker": worker}))
    entry = harness.entry("agent:demo:worker")
    assert entry is not None and entry.revision == 1

    with pytest.raises(PluginLoadError):
        harness.agents.install(
            AgentSpec(
                name="other",
                revision="1",
                plugins={"worker": worker, "zzz-broken": NotAPlugin},
            )
        )

    assert harness.entry("agent:demo:worker") is entry
    assert harness.entry("agent:demo:worker").revision == 1  # type: ignore[union-attr]
    assert harness.entry("agent:other:worker") is None
    assert harness.entry("agent:other:zzz-broken") is None


async def test_a_failed_materialization_leaves_no_revision_behind() -> None:
    harness = TestHarness()

    with pytest.raises(PluginLoadError):
        harness.agents.install(
            AgentSpec(
                name="demo",
                revision="1",
                plugins={"worker": worker, "zzz-broken": NotAPlugin},
            )
        )

    revision = harness.agents.install(
        AgentSpec(name="demo", revision="1", plugins={"worker": worker})
    )
    entry = harness.entry(revision.entries[0])
    assert entry is not None
    # The failed attempt left no revision behind: this starts back at 1.
    assert entry.revision == 1
