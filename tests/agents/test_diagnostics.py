"""Diagnostics for agent composition: explain and revision diff."""

from __future__ import annotations

from typing import Any

import pytest

from chassis import Harness, PluginContext, plugin
from chassis.agent_spec import AgentSpec
from chassis.agents import AgentNotFound
from chassis.capabilities.keys import CapabilityKey

TOOLS = CapabilityKey("tools", "1")
DATABASE = CapabilityKey("database", "1")


class StubTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = name

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
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


async def test_explain_agent_reports_composition_and_provenance() -> None:
    h = harness()
    h.agents.install(
        AgentSpec(
            name="finance",
            revision="17",
            runtime_ref="finance-graph",
            profile="reasoning",
            requires={"database": ">=1,<2"},
            tools=["web"],
            plugins={"ledger": {"api_key": "super-secret"}, "web": {}},
        )
    )
    try:
        await h.start()
        explanation = h.diagnostics.explain_agent("finance")

        assert explanation.identity == "finance@17"
        assert explanation.scope_path == "/agents/finance"
        assert explanation.runtime_ref == "finance-graph"
        assert explanation.profile == "reasoning"
        assert explanation.tools == ("web",)
        assert explanation.plugins == ("ledger", "web")
        assert explanation.plugin_entries == ("agent:finance:ledger", "agent:finance:web")
        assert explanation.visible_tools == ("web",)
        assert explanation.visible_providers["database"]
        assert [item.capability for item in explanation.requirements] == ["database"]
        assert explanation.requirements[0].status == "resolved"
        assert explanation.unresolved == ()
        assert explanation.generations == (h.current_generation.generation_id,)  # type: ignore[union-attr]
        assert explanation.composition_digest
        assert explanation.retired is False
        # Configuration is reported by key, never by value.
        assert "super-secret" not in str(explanation.to_dict())
        assert "super-secret" not in explanation.to_text()
    finally:
        await h.stop()


async def test_explain_agent_reaches_historical_revisions() -> None:
    h = harness()
    h.agents.install(AgentSpec(name="finance", revision="17", plugins={"ledger": {}}))
    try:
        await h.start()
        h.agents.replace(AgentSpec(name="finance", revision="18", plugins={"ledger": {}}))
        await h.reconcile()

        old = h.diagnostics.explain_agent("finance", revision="17")
        new = h.diagnostics.explain_agent("finance", revision="18")

        assert old.revision == "17"
        assert new.revision == "18"
        assert old.identity != new.identity
    finally:
        await h.stop()


async def test_explain_agent_rejects_unknown_agents() -> None:
    h = harness()
    with pytest.raises(AgentNotFound):
        h.diagnostics.explain_agent("missing")


async def test_diff_agents_reports_intent_and_composition_impact() -> None:
    h = harness()
    h.agents.install(
        AgentSpec(
            name="finance",
            revision="17",
            capabilities=["database", "tools"],
            tools=["web"],
            plugins={"ledger": {}},
        )
    )
    try:
        await h.start()
        h.agents.replace(
            AgentSpec(
                name="finance",
                revision="18",
                capabilities=["database", "tools"],
                tools=["web", "notes"],
                plugins={"ledger": {}, "web": {}},
            )
        )
        await h.reconcile()

        diff = h.diagnostics.diff_agents("finance", "17", "18")

        assert [(item.kind, item.subject) for item in diff.tool_changes] == [("added", "notes")]
        assert [item.kind for item in diff.plugin_changes] == ["added"]
        assert diff.plugin_changes[0].subject == "web"
        assert diff.scope_changes == ()
        assert diff.runtime_changes == ()

        # The composition impact delegates to the reuse engine: the unchanged
        # ledger contribution is reused, the new one is added.
        assert diff.impact is not None
        ledger = diff.impact.get("agent:finance:ledger")
        assert ledger is not None and ledger.decision == "reused"
        web = diff.impact.get("agent:finance:web")
        assert web is not None and web.decision == "added"
        assert "COMPOSITION IMPACT" in diff.to_text()
    finally:
        await h.stop()


async def test_diff_agents_compares_capability_and_requirement_views() -> None:
    h = harness()
    h.agents.install(
        AgentSpec(
            name="finance",
            revision="17",
            capabilities=["database", "tools"],
            requires={"database": ">=1,<2"},
            plugins={"ledger": {}},
        )
    )
    try:
        await h.start()
        h.agents.replace(
            AgentSpec(
                name="finance",
                revision="18",
                capabilities=["database"],
                requires={"database": ">=1,<3"},
                plugins={"ledger": {}},
            )
        )
        await h.reconcile()

        diff = h.diagnostics.diff_agents("finance", "17", "18")

        assert [(item.kind, item.subject) for item in diff.capability_changes] == [
            ("removed", "tools")
        ]
        assert [(item.kind, item.subject) for item in diff.requirement_changes] == [
            ("changed", "requires:database")
        ]
        assert diff.by_category("capability")
    finally:
        await h.stop()
