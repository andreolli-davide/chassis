"""Tool visibility inside composition scopes.

Tools follow the same inheritance-plus-narrowing rule as capabilities: a scope
sees the tools of its own entries and its ancestors, filtered by the intersection
of every tool view along its path. Sibling-local tools stay invisible.
"""

from __future__ import annotations

from typing import Any

from chassis import Harness, PluginContext, plugin
from chassis.capabilities.keys import CapabilityKey

TOOLS = CapabilityKey("tools", "1")


class StubTool:
    """A minimal structural tool: a name, a description, an awaitable invoke."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"stub {name}"

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        return input


def toolbox(name: str, *tool_names: str):  # type: ignore[no-untyped-def]
    """A plugin that registers one tool per name."""

    @plugin(name=name, version="1.0.0", provides={"tools": "1.0.0"})
    async def box(ctx: PluginContext) -> None:
        for tool_name in tool_names:
            ctx.tools.register(StubTool(tool_name))
        ctx.capabilities.provide(TOOLS, tuple(tool_names))

    return box


async def test_a_child_scope_inherits_ancestor_tools_by_default() -> None:
    harness = Harness()
    harness.install(toolbox("shared", "search"), entry_id="shared")
    research = harness.composition.child("research")
    research.install(toolbox("local", "notes"), entry_id="notes")
    try:
        await harness.start()
        generation = harness.current_generation
        assert generation is not None

        root = generation.scopes.get("/")
        child = generation.scopes.get("/research")

        assert root is not None and child is not None
        assert root.local_tools == ("search",)
        assert root.visible_tools == ("search",)
        assert child.local_tools == ("notes",)
        assert child.inherited_tools == ("search",)
        assert child.visible_tools == ("notes", "search")
        assert child.tools is None
    finally:
        await harness.stop()


async def test_a_tool_view_narrows_and_siblings_stay_isolated() -> None:
    harness = Harness()
    harness.install(toolbox("shared", "search", "telemetry"), entry_id="shared")
    research = harness.composition.child("research", tools=["search"])
    research.install(toolbox("research-tools", "notes"), entry_id="research-tools")
    finance = harness.composition.child("finance")
    finance.install(toolbox("finance-tools", "ledger"), entry_id="finance-tools")
    try:
        await harness.start()
        generation = harness.current_generation
        assert generation is not None

        research_scope = generation.scopes.get("/research")
        finance_scope = generation.scopes.get("/finance")
        assert research_scope is not None and finance_scope is not None

        # The view is a ceiling: neither the local tool outside it nor the
        # inherited one outside it is visible.
        assert research_scope.tools == ("search",)
        assert research_scope.visible_tools == ("search",)
        assert "notes" not in research_scope.visible_tools
        assert "telemetry" not in research_scope.visible_tools

        # Sibling-local tools are never visible.
        assert finance_scope.visible_tools == ("ledger", "search", "telemetry")
        assert "ledger" not in research_scope.visible_tools
        assert "notes" not in finance_scope.visible_tools
    finally:
        await harness.stop()


async def test_tool_snapshot_and_run_environment_honour_the_scope() -> None:
    harness = Harness()
    harness.install(toolbox("shared", "search"), entry_id="shared")
    harness.composition.child("research", tools=["search"])
    try:
        await harness.start()
        generation = harness.current_generation
        assert generation is not None

        assert harness.tool_snapshot(generation).names == ("search",)
        assert harness.tool_snapshot(generation, scope="/research").names == ("search",)
        assert harness.run_environment(generation, scope="/research").tools.names == ("search",)

        # A scope that exposes no tools sees none, rather than leaking the
        # generation's whole tool set.
        harness.composition.child("locked", tools=[])
        await harness.reconcile()
        narrowed = harness.current_generation
        assert narrowed is not None
        assert harness.tool_snapshot(narrowed, scope="/locked").names == ()
    finally:
        await harness.stop()


async def test_a_tool_view_change_publishes_a_new_generation() -> None:
    harness = Harness()
    harness.install(toolbox("shared", "search", "notes"), entry_id="shared")
    scope = harness.composition.child("research")
    try:
        await harness.start()
        before = harness.current_generation
        assert before is not None

        scope.select_tools("search")
        after = await harness.reconcile()

        current = harness.current_generation
        assert current is not None
        assert current is not before
        assert after.generation_id == current.generation_id
        resolved = current.scopes.get("/research")
        assert resolved is not None
        assert resolved.visible_tools == ("search",)
    finally:
        await harness.stop()


async def test_explain_scope_reports_owned_and_visible_tools() -> None:
    harness = Harness()
    harness.install(toolbox("shared", "search"), entry_id="shared")
    harness.composition.child("research", tools=["search"])
    try:
        await harness.start()

        explanation = harness.diagnostics.explain_scope("/research")

        assert explanation.visible_tools == ("search",)
        assert "visible tools: search" in explanation.to_text()
    finally:
        await harness.stop()
