from __future__ import annotations

import pytest

from chassis import MODEL, PluginContext, plugin
from chassis.budget import BudgetDimension, BudgetGovernor, BudgetLimits
from chassis.core.errors import BudgetExceeded, PluginSetupError
from chassis.testing import TestHarness, fake_tool
from chassis.tools import ToolExecutor, ToolRequest


def database_plugin():  # type: ignore[no-untyped-def]
    @plugin(name="postgres", version="1.0.0", provides={"database": "1.0.0"})
    async def postgres(ctx: PluginContext) -> None:
        ctx.capabilities.provide(
            __import__("chassis.capabilities", fromlist=["DATABASE"]).DATABASE, "db"
        )

    return postgres


async def test_reconcile_emits_spans_and_control_plane_events() -> None:
    async with TestHarness(plugins=[database_plugin()]) as harness:
        assert "harness.reconcile" in harness.recorded_spans
        assert {"dependency.resolve", "generation.build", "generation.publish"} <= set(
            harness.recorded_events
        )

        reconcile = harness.telemetry.spans_named("harness.reconcile")[0]
        assert reconcile.attributes["harness"] == "test-harness"
        assert reconcile.attributes["generation_id"] == "gen_0001"
        assert reconcile.attributes["mounted"] == 1

        published = next(
            event for event in harness.telemetry.events if event.name == "generation.publish"
        )
        assert published.attributes["generation_id"] == "gen_0001"
        assert published.attributes["previous"] is None


async def test_plugin_mount_and_unmount_are_instrumented() -> None:
    async with TestHarness(plugins=[database_plugin()]) as harness:
        mounts = harness.telemetry.spans_named("plugin.mount")
        assert [record.attributes["plugin"] for record in mounts] == ["postgres"]
        assert mounts[0].attributes["instance_id"].startswith("plugin_")

    unmounts = harness.telemetry.spans_named("plugin.unmount")
    assert [record.attributes["plugin"] for record in unmounts] == ["postgres"]
    assert "harness.shutdown" in harness.recorded_spans


async def test_no_op_reconcile_still_instruments_but_publishes_nothing() -> None:
    async with TestHarness(plugins=[database_plugin()]) as harness:
        harness.telemetry.clear()
        await harness.reconcile()

        assert harness.recorded_spans == ["harness.reconcile"]
        assert "generation.publish" not in harness.recorded_events


async def test_generation_retirement_is_instrumented_when_a_provider_is_removed() -> None:
    async with TestHarness(plugins=[database_plugin()]) as harness:
        harness.telemetry.clear()
        harness.uninstall("plugin-1")
        await harness.reconcile()

        retired = [
            event for event in harness.telemetry.events if event.name == "generation.retired"
        ]
        assert [event.attributes["generation_id"] for event in retired] == ["gen_0001"]
        assert [
            record.attributes["plugin"]
            for record in harness.telemetry.spans_named("plugin.unmount")
        ] == ["postgres"]


async def test_budget_exhaustion_is_instrumented_at_the_tool_boundary() -> None:
    async with TestHarness(
        tools=[fake_tool("echo", result="ok", parameters={"text": (str, ...)})]
    ) as harness:
        generation = harness.current_generation
        assert generation is not None
        harness.telemetry.clear()
        executor = ToolExecutor(telemetry=harness.telemetry)
        budget = BudgetGovernor(BudgetLimits(tool_calls=0))

        with pytest.raises(BudgetExceeded):
            await executor.execute(
                ToolRequest(name="echo", args={"text": "hi"}),
                snapshot=harness.tool_snapshot(generation),
                budget=budget,
            )

        exhausted = [
            event for event in harness.telemetry.events if event.name == "budget.exhausted"
        ]
        assert [event.attributes["dimension"] for event in exhausted] == [
            BudgetDimension.TOOL_CALLS.value
        ]


async def test_failed_reconcile_records_the_error_on_its_span() -> None:
    @plugin(name="broken", version="1.0.0")
    async def broken(ctx: PluginContext) -> None:
        raise RuntimeError("setup exploded")

    harness = TestHarness()
    harness.install(broken, entry_id="broken")

    with pytest.raises(PluginSetupError):
        await harness.reconcile()

    spans = harness.telemetry.spans_named("harness.reconcile")
    assert spans
    assert any("PluginSetupError" in error for error in spans[-1].errors)

    await harness.stop()


async def test_invocation_span_carries_generation_identity() -> None:
    from tests.test_runtime import StubRuntime

    async with TestHarness() as harness:
        harness.provide(MODEL, "model")
        harness.register_agent(StubRuntime())
        harness.telemetry.clear()

        await harness.agents.invoke("stub", {"messages": []})

        span = harness.telemetry.spans_named("agent.run")[0]
        assert span.attributes["agent"] == "stub"
        assert span.attributes["generation_id"] == harness.current_generation.generation_id  # type: ignore[union-attr]
