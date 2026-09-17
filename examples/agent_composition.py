"""Agent composition --- first-class, versioned composition for agents.

Chassis 0.5 lets a *logical agent* be described once and versioned, without
turning Chassis into an agent framework. This example builds three agents that
share infrastructure but see different composition:

    root                     shared database, model, telemetry
    ├── /agents/research     view: model, database, tools   tools: web-search
    ├── /agents/finance      view: model, database, tools   tools: spreadsheet
    └── /agents/support      view: model, tools             tools: ticket-lookup

It then shows an agent revision changing an unchanged contribution, a run staying
pinned to the revision it started under, and retirement.

Run it with::

    uv run python examples/agent_composition.py

The script asserts what it prints, so running it verifies the claims.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from chassis import DATABASE, MODEL, SCHEDULER, Harness, PluginContext, plugin
from chassis.agent_spec import AgentSpec
from chassis.agents import AgentRetired
from chassis.capabilities import CapabilityKey
from chassis.runtime import AgentEvent, AgentRequest, AgentResult, HarnessRunContext

TOOLS = CapabilityKey("tools", "1")
RESEARCH = "/agents/research"
FINANCE = "/agents/finance"
SUPPORT = "/agents/support"


class StubTool:
    """A minimal structural tool: name, description, awaitable invoke."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"the {name} tool"

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        return input


def provider(name: str, capability: CapabilityKey, value: object):  # type: ignore[no-untyped-def]
    """A plugin providing one capability."""

    @plugin(name=name, version="1.0.0", provides={capability.name: "1.0.0"})
    async def provide(ctx: PluginContext) -> None:
        ctx.capabilities.provide(capability, value)

    return provide


def tool_provider(name: str, *tool_names: str):  # type: ignore[no-untyped-def]
    """A plugin contributing one tool per name."""

    @plugin(name=name, version="1.0.0", provides={"tools": "1.0.0"})
    async def provide(ctx: PluginContext) -> None:
        for tool_name in tool_names:
            ctx.tools.register(StubTool(tool_name))
        ctx.capabilities.provide(TOOLS, tuple(tool_names))

    return provide


class EchoRuntime:
    """A backend-agnostic runtime: Chassis owns composition, this owns execution.

    A real integration would build a graph here; the point is that ``AgentSpec``
    references it by a logical name and never imports an execution engine.
    """

    name = "graph"

    async def invoke(self, request: AgentRequest, run_context: HarnessRunContext) -> AgentResult:
        return AgentResult(
            agent=run_context.agent,
            generation_id=run_context.generation_id,
            run_id=run_context.run_id,
            agent_revision=run_context.agent_revision,
            output={"agent_identity": run_context.agent_identity},
        )

    async def stream(
        self, request: AgentRequest, run_context: HarnessRunContext
    ) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(
            agent=run_context.agent,
            generation_id=run_context.generation_id,
            run_id=run_context.run_id,
            kind="done",
            agent_revision=run_context.agent_revision,
        )


def define_agents(harness: Harness) -> None:
    harness.agents.install(
        AgentSpec(
            name="research",
            revision="7",
            runtime_ref="graph",
            capabilities=["model", "database", "tools"],
            requires={"model": ">=1,<2", "database": ">=1,<2"},
            tools=["web-search"],
            profile="reasoning",
            plugins={"search": {}},
            metadata={"team": "research"},
        )
    )
    harness.agents.install(
        AgentSpec(
            name="finance",
            revision="17",
            runtime_ref="graph",
            capabilities=["model", "database", "tools"],
            requires={"model": ">=1,<2", "database": ">=1,<2"},
            tools=["spreadsheet"],
            profile="reasoning",
            plugins={"ledger": {}},
            metadata={"team": "finance"},
        )
    )
    harness.agents.install(
        AgentSpec(
            name="support",
            revision="3",
            runtime_ref="graph",
            capabilities=["model", "tools"],
            requires={"model": ">=1,<2"},
            tools=["ticket-lookup"],
            profile="fast",
            plugins={"tickets": {}},
            metadata={"team": "support"},
        )
    )


async def main() -> None:
    harness = Harness(name="agent-composition")

    # Shared infrastructure: every agent scope inherits it, filtered by its view.
    harness.install(provider("postgres-main", DATABASE, "postgres://shared"), entry_id="db")
    harness.install(provider("model-v1", MODEL, "model://v1"), entry_id="model")
    harness.install(provider("otel", SCHEDULER, "telemetry"), entry_id="telemetry")

    # Plugin contributions are resolved through the same catalog declarative
    # configuration uses.
    harness.register_plugin_type("search", tool_provider("search", "web-search"))
    harness.register_plugin_type("ledger", tool_provider("ledger", "spreadsheet"))
    harness.register_plugin_type("tickets", tool_provider("tickets", "ticket-lookup"))
    harness.register_plugin_type("market-data", tool_provider("market-data", "market-data"))
    harness.register_agent(EchoRuntime())

    define_agents(harness)

    async with harness as h:
        generation = generation_of(h)
        print(f"[start] generation={generation.generation_id}")

        # Materialization: each agent owns a composition scope, and its views
        # narrow what it may observe.
        research = scope_of(generation, RESEARCH)
        finance = scope_of(generation, FINANCE)
        support = scope_of(generation, SUPPORT)
        print(
            f"[research] tools={list(research.visible_tools)} providers={sorted(research.visible)}"
        )
        print(f"[finance]  tools={list(finance.visible_tools)} providers={sorted(finance.visible)}")
        print(f"[support]  tools={list(support.visible_tools)} providers={sorted(support.visible)}")

        assert research.visible_tools == ("web-search",)
        assert finance.visible_tools == ("spreadsheet",)
        assert support.visible_tools == ("ticket-lookup",)
        # Shared infrastructure is inherited; the support view hides `scheduler`.
        assert "database" in research.visible and "database" in finance.visible
        assert "database" not in support.visible
        assert "scheduler" not in support.visible
        assert research.metadata["chassis.agent_revision"] == "7"

        # Attribution: a run reports agent + revision + generation.
        result = await h.agents.invoke("finance", {"messages": []})
        print(
            f"[run] agent={result.agent} revision={result.agent_revision} "
            f"gen={result.generation_id}"
        )
        assert (result.agent, result.agent_revision) == ("finance", "17")
        assert result.output["agent_identity"] == "finance@17"

        # Diagnostics explain the composition from authoritative state.
        explanation = h.diagnostics.explain_agent("finance", revision="17")
        print("[explain]")
        print("  " + explanation.to_text().replace("\n", "\n  "))
        assert explanation.scope_path == FINANCE
        assert explanation.visible_tools == ("spreadsheet",)

        # A pinned run keeps its revision while a new one is published.
        entered = asyncio.Event()
        release = asyncio.Event()

        async def run_a() -> str:
            async with h.acquire() as pinned:
                entered.set()
                await release.wait()
                scope = pinned.scopes.get(FINANCE)
                assert scope is not None
                return scope.metadata["chassis.agent_revision"]

        task = asyncio.create_task(run_a())
        await entered.wait()

        h.agents.replace(
            AgentSpec(
                name="finance",
                revision="18",
                runtime_ref="graph",
                capabilities=["model", "database", "tools"],
                requires={"model": ">=1,<2", "database": ">=1,<2"},
                tools=["spreadsheet", "market-data"],
                profile="reasoning",
                plugins={"ledger": {}, "market-data": {}},
                metadata={"team": "finance"},
            )
        )
        result = await h.reconcile()
        print(f"[revision] mounted={list(result.mounted)} reused={len(result.reused)}")

        # Only the added contribution is mounted; the unchanged ledger is reused.
        assert result.mounted == ("agent:finance:market-data",)
        assert "agent:finance:ledger" in result.reused
        assert result.impact is not None
        ledger = result.impact.get("agent:finance:ledger")
        assert ledger is not None and ledger.decision == "reused"

        # The old run is still on revision 17; new runs select 18.
        release.set()
        released = await task
        print(f"[pinned] run A stayed on finance@{released}")
        assert released == "17"
        assert h.diagnostics.explain_agent("finance").revision == "18"

        # Diff the two revisions: intent plus composition impact.
        diff = h.diagnostics.diff_agents("finance", "17", "18")
        print("[diff]")
        print("  " + diff.to_text().replace("\n", "\n  "))
        assert [item.subject for item in diff.tool_changes] == ["market-data"]
        assert [item.subject for item in diff.plugin_changes] == ["market-data"]

        # Retirement: no new run selects the agent, and its scope leaves the next
        # generation without disturbing published generations.
        assert h.agents.remove("support") is True
        await h.reconcile()
        try:
            await h.agents.invoke("support", {"messages": []})
        except AgentRetired:
            print("[retire] support is retired; new runs refuse it")
        else:  # pragma: no cover - the assertion below is the real check
            raise AssertionError("a retired agent was selected for a new run")
        assert h.current_generation is not None
        assert h.current_generation.scopes.get(SUPPORT) is None
        assert h.agents.spec("support", revision="3").identity == "support@3"

    print("\nAgent composition completed: materialization, narrowing, revision pinning,")
    print("incremental reuse, diagnostics, and retirement all verified.")


def generation_of(harness: Harness):  # type: ignore[no-untyped-def]
    generation = harness.current_generation
    assert generation is not None
    return generation


def scope_of(generation, path: str):  # type: ignore[no-untyped-def]
    scope = generation.scopes.get(path)
    assert scope is not None, path
    return scope


if __name__ == "__main__":
    asyncio.run(main())
