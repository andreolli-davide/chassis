"""Agent revisioning benefits from 0.4 incremental reuse, and stays concurrent.

A revision change is a composition change like any other: unrelated agent scopes
are reused, a shared provider is retained, and the affected scope rebuilds only
the dependency closure that actually changed.
"""

from __future__ import annotations

import asyncio

from chassis import Harness, PluginContext, plugin
from chassis.agent_spec import AgentSpec
from chassis.capabilities.keys import CapabilityKey

DATABASE = CapabilityKey("database", "1")
TOOLS = CapabilityKey("tools", "1")

AGENTS = 100


class StubTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = name

    async def ainvoke(self, input: object, config: object = None, **kwargs: object) -> object:
        return input


def ledger_plugin():  # type: ignore[no-untyped-def]
    @plugin(name="ledger", version="1.0.0", provides={"database": "1.0.0"})
    async def ledger(ctx: PluginContext) -> None:
        ctx.capabilities.provide(DATABASE, "ledger-db")

    return ledger


def web_plugin():  # type: ignore[no-untyped-def]
    @plugin(name="web", version="1.0.0", provides={"tools": "1.0.0"})
    async def web(ctx: PluginContext) -> None:
        ctx.tools.register(StubTool("web"))
        ctx.capabilities.provide(TOOLS, ("web",))

    return web


def harness() -> Harness:
    h = Harness(name="agents")
    h.register_plugin_type("ledger", ledger_plugin())
    h.register_plugin_type("web", web_plugin())
    return h


def entry(agent: str, plugin_name: str) -> str:
    return f"agent:{agent}:{plugin_name}"


async def test_one_agent_revision_change_among_many_rebuilds_only_the_change() -> None:
    h = harness()
    h.install(ledger_plugin(), entry_id="shared-db")
    for index in range(AGENTS):
        h.agents.install(AgentSpec(name=f"agent{index:03d}", revision="1", plugins={"ledger": {}}))
    try:
        await h.start()
        shared_before = h.plugin_registry.instance("shared-db")
        assert shared_before is not None
        before = {
            index: h.plugin_registry.instance(entry(f"agent{index:03d}", "ledger"))
            for index in range(AGENTS)
        }

        h.agents.replace(
            AgentSpec(name="agent042", revision="2", plugins={"ledger": {}, "web": {}})
        )
        result = await h.reconcile()

        # Only the changed agent's new contribution was mounted.
        assert result.mounted == (entry("agent042", "web"),)
        assert result.impact is not None
        counts = result.impact.counts()
        assert counts.get("reused", 0) >= AGENTS
        assert counts.get("added", 0) == 1

        # The shared provider and every unrelated agent instance were retained.
        shared_after = h.plugin_registry.instance("shared-db")
        assert shared_after is not None
        assert shared_after.instance_id == shared_before.instance_id
        for index in range(AGENTS):
            instance = h.plugin_registry.instance(entry(f"agent{index:03d}", "ledger"))
            assert instance is not None
            assert instance.instance_id == before[index].instance_id  # type: ignore[union-attr]
    finally:
        await h.stop()


async def test_publication_while_an_old_revision_run_is_active() -> None:
    h = harness()
    h.agents.install(AgentSpec(name="finance", revision="17", plugins={"ledger": {}}))
    try:
        await h.start()
        generation_17 = h.current_generation
        assert generation_17 is not None

        released = asyncio.Event()
        entered = asyncio.Event()

        async def run_a() -> str:
            async with h.acquire() as pinned:
                entered.set()
                await released.wait()
                return pinned.scopes.get("/agents/finance").metadata[  # type: ignore[union-attr]
                    "chassis.agent_revision"
                ]

        task = asyncio.create_task(run_a())
        await entered.wait()

        h.agents.replace(
            AgentSpec(name="finance", revision="18", plugins={"ledger": {}, "web": {}})
        )
        await h.reconcile()
        generation_18 = h.current_generation
        assert generation_18 is not None and generation_18 is not generation_17

        # Each generation keeps its own revision, and the reused ledger instance
        # is shared by both while the old run is still active.
        scope_17 = generation_17.scopes.get("/agents/finance")
        scope_18 = generation_18.scopes.get("/agents/finance")
        assert scope_17 is not None and scope_18 is not None
        assert scope_17.metadata["chassis.agent_revision"] == "17"
        assert scope_18.metadata["chassis.agent_revision"] == "18"
        assert h.plugin_registry.instance(entry("finance", "ledger")) is not None
        assert h.plugin_registry.instance(entry("finance", "web")) is not None

        released.set()
        assert await task == "17"
    finally:
        await h.stop()
