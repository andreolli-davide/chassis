"""Resource counters prove a lifecycle returns to baseline (roadmap R029).

`Diagnostics.resource_counts()` reads authoritative state — owned scopes,
effects, tasks, stragglers, cleanup failures, instances, leases, and live
generations — so a run cycle can be bracketed by two reads and the difference
proved zero. These are the counters the stress and soak suites use the same way.
"""

from __future__ import annotations

from chassis import ResourceCounts
from chassis.testing import TestHarness, fake_tool
from chassis.tools import ToolRequest


async def test_resource_counts_return_to_baseline_after_a_run_cycle() -> None:
    harness = TestHarness()
    harness.install_tools(fake_tool("echo", result="hi", parameters={"text": (str, ...)}))

    before = harness.diagnostics.resource_counts()
    assert before.instances == 0
    assert before.leases == 0
    assert before.live_generations == 0

    await harness.start()
    try:
        generation = harness.current_generation
        assert generation is not None
        async with harness.acquire():
            during = harness.diagnostics.resource_counts()
            assert during.instances > 0
            assert during.scopes > before.scopes
            assert during.effects > 0
            assert during.leases == 1
            assert during.live_generations == 1

            result = await harness.tool_executor.execute(
                ToolRequest(name="echo", args={"text": "hi"}),
                snapshot=harness.tool_snapshot(generation),
            )
            assert result is not None
    finally:
        await harness.stop()

    after = harness.diagnostics.resource_counts()
    assert after.to_dict() == before.to_dict(), (before.to_dict(), after.to_dict())


async def test_resource_counts_reflect_work_in_progress() -> None:
    async with TestHarness() as harness:
        harness.install_tools(fake_tool("echo", result="hi", parameters={"text": (str, ...)}))
        await harness.reconcile()

        counts = harness.diagnostics.resource_counts()
        assert counts.entries > 0
        assert counts.instances > 0
        assert counts.tasks == 0
        assert counts.stragglers == 0
        assert counts.cleanup_failures == 0
        assert counts.live_generations == 1
        assert counts.draining_generations == 0

        payload: dict[str, int] = counts.to_dict()
        assert set(payload) == {
            "entries",
            "instances",
            "scopes",
            "effects",
            "tasks",
            "stragglers",
            "cleanup_failures",
            "live_generations",
            "draining_generations",
            "leases",
        }
        assert isinstance(counts, ResourceCounts)
