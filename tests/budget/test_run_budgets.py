"""Budgets across agent runs, including runs started from inside another run."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from langchain_core.tools import tool

from chassis import Harness, PluginContext, plugin
from chassis.budget import BudgetLimits
from chassis.core.errors import BudgetExceeded
from chassis.runtime import AgentEvent, AgentRequest, AgentResult, HarnessRunContext
from chassis.tools import ToolPolicy, ToolRequest


@tool
def echo(text: str) -> str:
    """Return the text unchanged."""

    return text


def echo_plugin():  # type: ignore[no-untyped-def]
    @plugin(name="echo-tool", version="1.0.0")
    async def echo_tools(ctx: PluginContext) -> None:
        ctx.tools.register(echo, policy=ToolPolicy())

    return echo_tools


class NestedRuntime:
    """Agent that starts ``child`` from inside its own run, ``times`` times."""

    def __init__(self, name: str, child: str, *, times: int = 1) -> None:
        self.name = name
        self.harness: Harness | None = None
        self._child = child
        self._times = times

    async def invoke(self, request: AgentRequest, run_context: HarnessRunContext) -> AgentResult:
        assert self.harness is not None
        for _ in range(self._times):
            await self.harness.agents.invoke(self._child, {"messages": []})
        return AgentResult(
            agent=self.name,
            generation_id=run_context.generation_id,
            run_id=run_context.run_id,
            output={},
        )

    async def stream(
        self, request: AgentRequest, run_context: HarnessRunContext
    ) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(
            agent=self.name,
            generation_id=run_context.generation_id,
            run_id=run_context.run_id,
            kind="values",
        )


class ToolCallingRuntime:
    """Agent that calls a mediated tool ``times`` times through its run context."""

    def __init__(self, name: str = "caller", *, times: int = 2) -> None:
        self.name = name
        self._times = times

    async def invoke(self, request: AgentRequest, run_context: HarnessRunContext) -> AgentResult:
        environment = run_context.environment
        assert environment is not None
        for index in range(self._times):
            await environment.executor.execute(
                ToolRequest(name="echo", args={"text": f"call-{index}"}),
                snapshot=environment.tools,
                budget=environment.budget,
                hook_snapshot=environment.hooks,
            )
        return AgentResult(
            agent=self.name,
            generation_id=run_context.generation_id,
            run_id=run_context.run_id,
            output={},
        )

    async def stream(
        self, request: AgentRequest, run_context: HarnessRunContext
    ) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(
            agent=self.name,
            generation_id=run_context.generation_id,
            run_id=run_context.run_id,
            kind="values",
        )


class LeafRuntime(ToolCallingRuntime):
    """Inner agent used by the nesting tests."""

    def __init__(self, name: str = "inner", *, times: int = 1) -> None:
        super().__init__(name, times=times)


async def test_nested_runs_are_charged_as_child_runs() -> None:
    harness = Harness(default_budget_limits=BudgetLimits(child_runs=1))
    harness.install(echo_plugin(), entry_id="echo-tool")
    await harness.start()
    try:
        outer = NestedRuntime("outer", "inner", times=2)
        outer.harness = harness
        harness.register_agent(outer)
        harness.register_agent(LeafRuntime("inner"))

        with pytest.raises(BudgetExceeded) as excinfo:
            await harness.agents.invoke("outer", {"messages": []})

        assert excinfo.value.context["dimension"] == "child_runs"
        assert excinfo.value.context["limit"] == 1
    finally:
        await harness.stop()


async def test_nested_run_inherits_the_parent_budget() -> None:
    """The child takes its own allocation, but the parent's remaining still binds it."""

    harness = Harness()
    harness.install(echo_plugin(), entry_id="echo-tool")
    await harness.start()
    try:
        outer = NestedRuntime("outer", "inner")
        outer.harness = harness
        harness.register_agent(outer)
        harness.register_agent(LeafRuntime("inner", times=2))

        # Only the outer run is budgeted; the nested run passes no limits of its own.
        with pytest.raises(BudgetExceeded) as excinfo:
            await harness.agents.invoke(
                "outer", {"messages": []}, limits=BudgetLimits(tool_calls=1)
            )

        assert excinfo.value.context["dimension"] == "tool_calls"
        assert excinfo.value.context["limit"] == 1
    finally:
        await harness.stop()


async def test_a_top_level_run_enforces_its_own_limits() -> None:
    harness = Harness(default_budget_limits=BudgetLimits(tool_calls=2))
    harness.install(echo_plugin(), entry_id="echo-tool")
    await harness.start()
    try:
        harness.register_agent(ToolCallingRuntime("caller", times=2))

        await harness.agents.invoke("caller", {"messages": []})

        harness.register_agent(ToolCallingRuntime("greedy", times=3), replace=False)
        with pytest.raises(BudgetExceeded):
            await harness.agents.invoke("greedy", {"messages": []})
    finally:
        await harness.stop()
