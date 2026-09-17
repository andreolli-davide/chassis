"""Budget enforcement semantics: what Chassis guarantees, and what it cannot.

A configured limit is only a guarantee when Chassis owns the boundary where the
dimension is consumed. ``model_calls``, ``tokens``, and ``estimated_cost`` are
produced inside the execution engine, so a limit on them is intent: it holds only
when the integration reports usage. These tests pin that distinction in the API and
in diagnostics, and prove that exceeding a limit stays deterministic at every
boundary.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from chassis import Harness, PluginContext, plugin
from chassis.budget import (
    BudgetDimension,
    BudgetEnforcement,
    BudgetGovernor,
    BudgetLimits,
)
from chassis.core.errors import BudgetExceeded
from chassis.runtime import AgentEvent, AgentRequest, AgentResult, HarnessRunContext
from chassis.tools import ToolPolicy, ToolRequest


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_every_dimension_declares_who_enforces_it() -> None:
    assert BudgetDimension.WALL_CLOCK_SECONDS.enforcement is BudgetEnforcement.ENFORCED
    assert BudgetDimension.TOOL_CALLS.enforcement is BudgetEnforcement.ENFORCED
    assert BudgetDimension.CHILD_RUNS.enforcement is BudgetEnforcement.ENFORCED

    assert BudgetDimension.MODEL_CALLS.enforcement is BudgetEnforcement.ACCOUNTED
    assert BudgetDimension.TOKENS.enforcement is BudgetEnforcement.ACCOUNTED
    assert BudgetDimension.ESTIMATED_COST.enforcement is BudgetEnforcement.ACCOUNTED

    assert BudgetDimension.TOOL_CALLS.is_enforced is True
    assert BudgetDimension.TOKENS.is_enforced is False


def test_limits_split_configured_dimensions_by_enforcement_mode() -> None:
    limits = BudgetLimits(tool_calls=5, tokens=1000, wall_clock_seconds=30.0)

    assert set(limits.enforced_dimensions()) == {
        BudgetDimension.TOOL_CALLS,
        BudgetDimension.WALL_CLOCK_SECONDS,
    }
    assert limits.accounted_dimensions() == (BudgetDimension.TOKENS,)
    assert limits.requires_accounting is True

    tokens = limits.spec_for(BudgetDimension.TOKENS)
    assert tokens.limit == 1000
    assert tokens.enforcement is BudgetEnforcement.ACCOUNTED
    assert tokens.is_enforced is False
    assert tokens.requires_accounting is True


def test_unconfigured_dimensions_are_neither_enforced_nor_accounted() -> None:
    limits = BudgetLimits(tokens=10)

    assert limits.enforced_dimensions() == ()
    assert limits.accounted_dimensions() == (BudgetDimension.TOKENS,)

    # `describe` carries every dimension, configured or not.
    described = limits.describe()
    assert set(described) == {dimension.value for dimension in BudgetDimension}
    assert described["tool_calls"]["limit"] is None
    assert described["tool_calls"]["enforcement"] == "enforced"
    assert BudgetLimits().requires_accounting is False


def test_unlimited_budget_needs_no_accounting() -> None:
    governor = BudgetGovernor()

    assert governor.describe()["tokens"]["enforcement"] == "accounted"
    assert governor.to_dict()["enforcement"]["tokens"] == "accounted"
    assert governor.to_dict()["enforcement"]["tool_calls"] == "enforced"


def test_enforced_dimensions_raise_deterministically_at_chassis_boundaries() -> None:
    clock = FakeClock()
    governor = BudgetGovernor(
        BudgetLimits(tool_calls=1, wall_clock_seconds=5.0, child_runs=1), clock=clock
    )

    governor.consume(BudgetDimension.TOOL_CALLS)
    with pytest.raises(BudgetExceeded) as tool_error:
        governor.consume(BudgetDimension.TOOL_CALLS)
    assert tool_error.value.context["dimension"] == "tool_calls"

    governor.child()
    with pytest.raises(BudgetExceeded) as child_error:
        governor.child()
    assert child_error.value.context["dimension"] == "child_runs"

    clock.advance(5.0)
    with pytest.raises(BudgetExceeded) as clock_error:
        governor.check(BudgetDimension.WALL_CLOCK_SECONDS)
    assert clock_error.value.context["dimension"] == "wall_clock_seconds"


def test_accounted_dimensions_are_charged_only_when_reported() -> None:
    governor = BudgetGovernor(BudgetLimits(tokens=100, model_calls=2, estimated_cost=0.05))

    # Nothing is charged until an integration reports it: a configured limit is
    # intent, not an observation Chassis can make on its own.
    assert governor.consumed(BudgetDimension.TOKENS) == 0
    assert governor.remaining(BudgetDimension.TOKENS) == 100

    governor.record(model_calls=1, tokens=60, estimated_cost=0.02)

    assert governor.consumed(BudgetDimension.MODEL_CALLS) == 1
    assert governor.consumed(BudgetDimension.TOKENS) == 60
    assert governor.remaining(BudgetDimension.ESTIMATED_COST) == pytest.approx(0.03)


def test_a_report_that_crosses_an_accounted_limit_is_refused() -> None:
    governor = BudgetGovernor(BudgetLimits(tokens=100))

    with pytest.raises(BudgetExceeded) as excinfo:
        governor.record(tokens=101)

    assert excinfo.value.context == {
        "dimension": "tokens",
        "limit": 100,
        "used": 0.0,
        "requested": 101.0,
    }
    # The refused report is not recorded.
    assert governor.consumed(BudgetDimension.TOKENS) == 0


def test_recorded_usage_propagates_to_the_parent_allocation() -> None:
    parent = BudgetGovernor(BudgetLimits(tokens=100))
    child = parent.child()

    child.record(model_calls=1, tokens=40)

    assert parent.consumed(BudgetDimension.TOKENS) == 40
    assert parent.consumed(BudgetDimension.MODEL_CALLS) == 1

    with pytest.raises(BudgetExceeded):
        child.record(tokens=61)


def test_record_with_nothing_to_report_is_a_no_op() -> None:
    governor = BudgetGovernor(BudgetLimits(tokens=1, model_calls=1, estimated_cost=0.01))

    governor.record()

    assert governor.consumed(BudgetDimension.TOKENS) == 0
    assert governor.remaining(BudgetDimension.TOKENS) == 1


async def test_an_integration_can_report_model_usage_from_a_run() -> None:
    class ReportingRuntime:
        """Agent that reports model usage the way an integration would."""

        def __init__(self, name: str, *, tokens: int) -> None:
            self.name = name
            self._tokens = tokens

        async def invoke(
            self, request: AgentRequest, run_context: HarnessRunContext
        ) -> AgentResult:
            budget = run_context.budget
            assert budget is not None
            for _ in range(self._tokens):
                budget.record(model_calls=1, tokens=1, estimated_cost=0.001)
            return AgentResult(
                agent=self.name,
                generation_id=run_context.generation_id,
                run_id=run_context.run_id,
                output={},
            )

        async def stream(
            self, request: AgentRequest, run_context: HarnessRunContext
        ) -> AsyncIterator[AgentEvent]:  # pragma: no cover - not exercised
            yield AgentEvent(
                agent=self.name,
                generation_id=run_context.generation_id,
                run_id=run_context.run_id,
                kind="values",
            )

    harness = Harness(default_budget_limits=BudgetLimits(tokens=3))
    harness.register_agent(ReportingRuntime("reporter", tokens=3))
    await harness.start()
    try:
        # Exactly at the limit is allowed.
        await harness.agents.invoke("reporter", {"messages": []})

        harness.register_agent(ReportingRuntime("greedy", tokens=4))
        with pytest.raises(BudgetExceeded) as excinfo:
            await harness.agents.invoke("greedy", {"messages": []})
        assert excinfo.value.context["dimension"] == "tokens"
    finally:
        await harness.stop()


async def test_an_unreported_accounted_limit_never_fires() -> None:
    """The honest failure mode: Chassis cannot enforce what it cannot observe."""

    class SilentRuntime:
        def __init__(self, name: str = "silent") -> None:
            self.name = name

        async def invoke(
            self, request: AgentRequest, run_context: HarnessRunContext
        ) -> AgentResult:
            return AgentResult(
                agent=self.name,
                generation_id=run_context.generation_id,
                run_id=run_context.run_id,
                output={},
            )

        async def stream(
            self, request: AgentRequest, run_context: HarnessRunContext
        ) -> AsyncIterator[AgentEvent]:  # pragma: no cover - not exercised
            yield AgentEvent(
                agent=self.name,
                generation_id=run_context.generation_id,
                run_id=run_context.run_id,
                kind="values",
            )

    harness = Harness(default_budget_limits=BudgetLimits(tokens=1))
    harness.register_agent(SilentRuntime())
    await harness.start()
    try:
        await harness.agents.invoke("silent", {"messages": []})
    finally:
        await harness.stop()


async def test_enforced_tool_calls_still_fire_for_a_budgeted_run() -> None:
    class EchoTool:
        """A tool is anything with a name, a description, and an async invoke."""

        name = "echo"
        description = "Return the text unchanged."

        async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> str:
            return str(input["text"])

    @plugin(name="echo-tool", version="1.0.0")
    async def echo_tools(ctx: PluginContext) -> None:
        ctx.tools.register(EchoTool(), policy=ToolPolicy())

    class Caller:
        def __init__(self, name: str = "caller") -> None:
            self.name = name

        async def invoke(
            self, request: AgentRequest, run_context: HarnessRunContext
        ) -> AgentResult:
            environment = run_context.environment
            assert environment is not None
            for _ in range(2):
                await environment.executor.execute(
                    ToolRequest(name="echo", args={"text": "a"}),
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
        ) -> AsyncIterator[AgentEvent]:  # pragma: no cover - not exercised
            yield AgentEvent(
                agent=self.name,
                generation_id=run_context.generation_id,
                run_id=run_context.run_id,
                kind="values",
            )

    harness = Harness(default_budget_limits=BudgetLimits(tool_calls=1))
    harness.install(echo_tools, entry_id="echo-tool")
    harness.register_agent(Caller())
    await harness.start()
    try:
        with pytest.raises(BudgetExceeded):
            await harness.agents.invoke("caller", {"messages": []})
    finally:
        await harness.stop()


async def test_diagnostics_explain_limits_and_enforcement_modes() -> None:
    harness = Harness(default_budget_limits=BudgetLimits(tool_calls=5, tokens=1000))
    await harness.start()
    try:
        report = harness.diagnostics.budgets()

        assert report["default_limits"]["tool_calls"] == 5
        assert report["default_limits"]["tokens"] == 1000
        assert report["enforced"] == ["tool_calls"]
        assert report["accounted"] == ["tokens"]
        assert report["requires_accounting"] is True
        assert report["dimensions"]["tokens"] == {
            "dimension": "tokens",
            "limit": 1000,
            "enforcement": "accounted",
        }
        assert report["dimensions"]["model_calls"]["limit"] is None
        assert report["dimensions"]["model_calls"]["enforcement"] == "accounted"
    finally:
        await harness.stop()


async def test_diagnostics_report_an_unconfigured_budget_honestly() -> None:
    harness = Harness()
    await harness.start()
    try:
        report = harness.diagnostics.budgets()

        assert set(report["default_limits"].values()) == {None}
        assert report["enforced"] == []
        assert report["accounted"] == []
        assert report["requires_accounting"] is False
    finally:
        await harness.stop()
