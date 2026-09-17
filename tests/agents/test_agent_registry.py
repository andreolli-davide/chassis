"""The agent revision registry: publication, materialization, retirement."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from chassis import Harness, PluginContext, plugin
from chassis.agent_spec import AgentSpec
from chassis.agents import AgentNotFound, AgentRetired
from chassis.capabilities.keys import CapabilityKey
from chassis.core.errors import ConfigurationError

TOOLS = CapabilityKey("tools", "1")
DATABASE = CapabilityKey("database", "1")


def ledger_plugin():  # type: ignore[no-untyped-def]
    @plugin(name="ledger", version="1.0.0", provides={"database": "1.0.0"})
    async def ledger(ctx: PluginContext) -> None:
        ctx.capabilities.provide(DATABASE, "ledger-db")

    return ledger


def toolbox_plugin():  # type: ignore[no-untyped-def]
    @plugin(name="web", version="1.0.0", provides={"tools": "1.0.0"})
    async def web(ctx: PluginContext) -> None:
        ctx.capabilities.provide(TOOLS, ("web",))

    return web


def harness() -> Harness:
    instance = Harness(name="agents")
    instance.register_plugin_type("ledger", ledger_plugin())
    instance.register_plugin_type("web", toolbox_plugin())
    return instance


async def test_install_materializes_a_scope_and_its_contributions() -> None:
    h = harness()
    revision = h.agents.install(
        AgentSpec(name="finance", revision="17", capabilities=["database"], plugins={"ledger": {}})
    )
    try:
        await h.start()
        generation = h.current_generation
        assert generation is not None

        scope = generation.scopes.get("/agents/finance")
        assert scope is not None
        assert scope.entries == ("agent:finance:ledger",)
        assert scope.metadata["chassis.agent"] == "finance"
        assert scope.metadata["chassis.agent_revision"] == "17"
        assert revision.identity == "finance@17"
        assert revision.entries == ("agent:finance:ledger",)
        assert h.entry("agent:finance:ledger") is not None
    finally:
        await h.stop()


async def test_unknown_plugin_reference_is_rejected() -> None:
    h = harness()
    with pytest.raises(ConfigurationError):
        h.agents.install(AgentSpec(name="finance", plugins={"missing": {}}))


async def test_a_published_revision_is_immutable() -> None:
    h = harness()
    h.agents.install(AgentSpec(name="finance", revision="17", plugins={"ledger": {}}))

    with pytest.raises(ConfigurationError):
        h.agents.install(AgentSpec(name="finance", revision="17", plugins={"web": {}}))


async def test_changing_the_revision_requires_replace() -> None:
    h = harness()
    h.agents.install(AgentSpec(name="finance", revision="17", plugins={"ledger": {}}))

    with pytest.raises(ConfigurationError):
        h.agents.install(AgentSpec(name="finance", revision="18", plugins={"ledger": {}}))

    revision = h.agents.replace(AgentSpec(name="finance", revision="18", plugins={"ledger": {}}))
    assert revision.identity == "finance@18"


async def test_unchanged_contributions_are_not_reinstalled_across_revisions() -> None:
    h = harness()
    h.agents.install(AgentSpec(name="finance", revision="17", plugins={"ledger": {}}))
    try:
        await h.start()
        before = h.plugin_registry.instance("agent:finance:ledger")
        assert before is not None

        h.agents.replace(
            AgentSpec(name="finance", revision="18", plugins={"ledger": {}, "web": {}})
        )
        result = await h.reconcile()

        after = h.plugin_registry.instance("agent:finance:ledger")
        assert after is not None
        # Same runtime instance: the unchanged contribution was reused, and only
        # the new one was mounted.
        assert after.instance_id == before.instance_id
        assert result.mounted == ("agent:finance:web",)
        assert result.impact is not None
        ledger = result.impact.get("agent:finance:ledger")
        assert ledger is not None and ledger.decision == "reused"
    finally:
        await h.stop()


async def test_retirement_stops_new_selection_and_keeps_old_generations() -> None:
    h = harness()
    h.agents.install(AgentSpec(name="finance", revision="17", plugins={"ledger": {}}))
    try:
        await h.start()
        assert h.agents.remove("finance") is True
        assert h.agents.is_retired("finance")
        assert h.agents.active_spec("finance") is None
        with pytest.raises(AgentRetired):
            h.agents.spec("finance")
        # The historical revision stays reachable for diagnostics.
        assert h.agents.spec("finance", revision="17").identity == "finance@17"

        # A run pinned to the pre-retirement generation keeps its composition and
        # its resources alive until it releases the lease.
        async with h.acquire() as pinned:
            assert pinned.scopes.get("/agents/finance") is not None
            await h.reconcile()
            assert h.current_generation is not pinned
            assert h.plugin_registry.instance("agent:finance:ledger") is not None

        # With the last lease gone, the retired generation's resource becomes
        # reachable by no live generation and is disposed.
        assert h.plugin_registry.instance("agent:finance:ledger") is None
    finally:
        await h.stop()


async def test_unknown_agent_and_revision_are_reported() -> None:
    h = harness()
    h.agents.install(AgentSpec(name="finance", revision="17"))

    with pytest.raises(AgentNotFound):
        h.agents.spec("unknown")
    with pytest.raises(AgentNotFound):
        h.agents.spec("finance", revision="99")


async def test_removing_and_republishing_the_same_name_is_allowed() -> None:
    h = harness()
    h.agents.install(AgentSpec(name="finance", revision="17", plugins={"ledger": {}}))
    h.agents.remove("finance")

    h.agents.install(AgentSpec(name="finance", revision="18", plugins={"ledger": {}}))

    assert h.agents.is_retired("finance") is False
    assert h.agents.active_spec("finance") is not None
    assert h.agents.revisions("finance") == ("17", "18")


async def test_materialized_capability_and_tool_views_narrow_the_scope() -> None:
    h = harness()
    h.install(ledger_plugin(), entry_id="root-ledger")
    h.agents.install(
        AgentSpec(
            name="finance",
            revision="17",
            capabilities=["database"],
            tools=["web"],
            plugins={"web": {}},
        )
    )
    try:
        await h.start()
        generation = h.current_generation
        assert generation is not None
        scope = generation.scopes.get("/agents/finance")
        assert scope is not None
        assert scope.capabilities == ("database",)
        assert scope.tools == ("web",)
    finally:
        await h.stop()


async def test_mutating_an_authoring_object_cannot_change_a_published_revision() -> None:
    config: dict[str, Any] = {"pool": {"size": 1}, "hosts": ["a"]}
    metadata: dict[str, Any] = {"team": "finance"}
    spec = AgentSpec(
        name="finance",
        revision="17",
        plugins={"ledger": config},
        metadata=metadata,
    )
    h = harness()
    h.agents.install(spec)
    try:
        await h.start()
        entry = h.entry("agent:finance:ledger")
        assert entry is not None

        def pool_size() -> object:
            pool = entry.config["pool"]
            assert isinstance(pool, Mapping)
            return pool["size"]

        def hosts() -> tuple[object, ...]:
            value = entry.config["hosts"]
            assert isinstance(value, tuple)
            return value

        assert pool_size() == 1
        assert hosts() == ("a",)

        # Mutating the objects the spec was built from must not reach the
        # materialized composition: the spec froze copies at construction.
        config["pool"]["size"] = 99
        config["hosts"].append("b")
        metadata["team"] = "other"

        assert pool_size() == 1
        assert hosts() == ("a",)
        scope = h.current_generation.scopes.get("/agents/finance")  # type: ignore[union-attr]
        assert scope is not None
        assert scope.metadata["team"] == "finance"

        # An unchanged reconcile still reuses the generation.
        before = h.current_generation
        await h.reconcile()
        assert h.current_generation is before
    finally:
        await h.stop()
