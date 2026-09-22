"""The signal contract holds across the runtime, backend-free (guarantee G27).

Every emission is validated against `chassis.telemetry.signals.SIGNALS` —
names, required attributes, and bounded cardinality — using only the in-repo
recording backend. Adapters are tested separately; the contract is what they
all transport.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from chassis import ChassisError, PluginContext, plugin
from chassis.core.errors import PolicyDenied
from chassis.replay import BoundaryKind, ReplayMode, ReplaySession, boundary_key
from chassis.telemetry.signals import (
    ATTRIBUTE_ITEMS_LIMIT,
    ATTRIBUTE_STRING_LIMIT,
    SIGNALS,
    validate_signal,
)
from chassis.testing import FakePolicy, TestHarness, fake_tool
from chassis.tools import ToolRequest


@plugin(name="contract-db", version="1.0.0", provides={"database": "1.0.0"})
async def db_plugin(ctx: PluginContext) -> None:
    from chassis import DATABASE

    ctx.capabilities.provide(DATABASE, "db-handle")


EXPECTED_SIGNALS = {
    "agent.run",
    "budget.exhausted",
    "cleanup.failure",
    "dependency.resolve",
    "generation.acquire",
    "generation.build",
    "generation.draining",
    "generation.impact",
    "generation.publish",
    "generation.release",
    "generation.retired",
    "graph.cache",
    "graph.cache.invalidate",
    "graph.compile",
    "harness.reconcile",
    "harness.shutdown",
    "hook.failure",
    "plugin.mount",
    "plugin.unmount",
    "policy.decision",
    "replay.exhausted",
    "replay.hit",
    "replay.miss",
    "telemetry.failure",
    "tool.execute",
}


def check_recording(recorder: Any) -> None:
    """Every recorded emission must conform and be a declared signal."""

    for record in (*recorder.spans, *recorder.events):
        problems = validate_signal(record.name, record.attributes)
        assert not problems, f"{record.name}: {problems}"


async def test_the_signal_registry_covers_every_required_operation() -> None:
    assert set(SIGNALS) == EXPECTED_SIGNALS
    for spec in SIGNALS.values():
        assert spec.name.count(" ") == 0
        assert spec.kind.value in {"span", "event"}


async def test_every_emitted_signal_conforms_to_the_contract() -> None:
    recorder: Any = None
    async with TestHarness(plugins=[db_plugin()]) as harness:
        recorder = harness.telemetry
        harness.install_tools(fake_tool("lookup", result="found", parameters={"term": (str, ...)}))
        await harness.reconcile()
        generation = harness.current_generation
        assert generation is not None
        async with harness.acquire():
            await harness.tool_executor.execute(
                ToolRequest(name="lookup", args={"term": "x"}),
                snapshot=harness.tool_snapshot(generation),
            )
        harness.uninstall("plugin-1")
        await harness.reconcile()

    check_recording(recorder)
    names = {*recorder.span_names(), *recorder.event_names()}
    expected = {"harness.reconcile", "tool.execute", "generation.acquire", "generation.release"}
    assert expected <= names
    assert "generation.publish" in recorder.event_names()
    assert "generation.retired" in recorder.event_names()


async def test_tool_signals_carry_the_correlation_fields() -> None:
    async with TestHarness() as harness:
        harness.install_tools(fake_tool("lookup", result="found", parameters={"term": (str, ...)}))
        await harness.reconcile()
        generation = harness.current_generation
        assert generation is not None
        harness.telemetry.clear()
        async with harness.acquire():
            await harness.tool_executor.execute(
                ToolRequest(name="lookup", args={"term": "x"}, run_id="run_7"),
                snapshot=harness.tool_snapshot(generation),
            )

        tool_span = harness.telemetry.spans_named("tool.execute")[0]
        assert tool_span.attributes["generation_id"] == generation.generation_id
        assert tool_span.attributes["run_id"] == "run_7"
        assert tool_span.attributes["registration_id"]
        acquired = [
            event for event in harness.telemetry.events if event.name == "generation.acquire"
        ]
        assert acquired[0].attributes["generation_id"] == generation.generation_id


async def test_replay_decisions_are_instrumented() -> None:
    recording = ReplaySession(mode=ReplayMode.RECORD)
    key = boundary_key(BoundaryKind.TOOL.value, "lookup", {"term": "x"})
    recording.record(
        BoundaryKind.TOOL,
        key=key,
        request={"tool": "lookup", "args": {"term": "x"}},
        response={"name": "lookup", "status": "ok", "content": "recorded"},
    )
    replaying = ReplaySession.from_dict(recording.to_dict(), mode=ReplayMode.REPLAY)

    async with TestHarness(replay=replaying) as harness:
        calls: list[tuple[str, dict[str, Any]]] = []
        harness.install_tools(
            fake_tool("lookup", result="live", parameters={"term": (str, ...)}, calls=calls)
        )
        await harness.reconcile()
        generation = harness.current_generation
        assert generation is not None
        harness.telemetry.clear()

        result = await harness.tool_executor.execute(
            ToolRequest(name="lookup", args={"term": "x"}),
            snapshot=harness.tool_snapshot(generation),
        )
        assert result is not None and result.content == "recorded"

        with pytest.raises(ChassisError):
            await harness.tool_executor.execute(
                ToolRequest(name="lookup", args={"term": "other"}),
                snapshot=harness.tool_snapshot(generation),
            )

        names = harness.telemetry.event_names()
        assert "replay.hit" in names
        assert "replay.miss" in names or "replay.exhausted" in names
        check_recording(harness.telemetry)


async def test_secrets_never_reach_a_signal() -> None:
    secret = "sk-live-signal-secret-42"

    async with TestHarness() as harness:
        harness.redactor.add(secret)
        harness.install_tools(
            fake_tool(
                "lookup",
                result=f"found {secret}",
                parameters={"term": (str, ...)},
            )
        )
        await harness.reconcile()
        generation = harness.current_generation
        assert generation is not None
        async with harness.acquire():
            await harness.tool_executor.execute(
                ToolRequest(name="lookup", args={"term": secret}),
                snapshot=harness.tool_snapshot(generation),
            )

        rendered = repr(
            [(record.name, dict(record.attributes)) for record in harness.telemetry.spans]
            + [(record.name, dict(record.attributes)) for record in harness.telemetry.events]
        )
        assert secret not in rendered


async def test_a_denial_is_instrumented_as_a_policy_decision() -> None:
    from chassis.tools import ToolPolicy

    async with TestHarness(policy=FakePolicy([])) as harness:
        harness.install_tools(
            fake_tool("rm", result="gone", parameters={"path": (str, ...)}),
            policies={"rm": ToolPolicy(permissions=("filesystem.delete",))},
        )
        await harness.reconcile()
        generation = harness.current_generation
        assert generation is not None
        harness.telemetry.clear()

        with pytest.raises(PolicyDenied):
            await harness.tool_executor.execute(
                ToolRequest(name="rm", args={"path": "/tmp/x"}),
                snapshot=harness.tool_snapshot(generation),
            )

        decisions = [event for event in harness.telemetry.events if event.name == "policy.decision"]
        assert decisions and decisions[-1].attributes["allowed"] is False
        check_recording(harness.telemetry)


def test_cardinality_bounds_reject_unbounded_payloads() -> None:
    problems = validate_signal(
        "tool.execute",
        {
            "tool": "lookup",
            "generation_id": "gen_0001",
            "request": {"messages": ["unbounded"]},
        },
    )
    assert any("unbounded payload" in problem for problem in problems)

    problems = validate_signal(
        "tool.execute",
        {"tool": "lookup", "generation_id": "gen_0001", "note": "x" * (ATTRIBUTE_STRING_LIMIT + 1)},
    )
    assert any("exceeds" in problem for problem in problems)

    problems = validate_signal(
        "tool.execute",
        {
            "tool": "lookup",
            "generation_id": "gen_0001",
            "items": list(range(ATTRIBUTE_ITEMS_LIMIT + 1)),
        },
    )
    assert any("items" in problem for problem in problems)

    assert validate_signal("not.a.signal", {}) != ()


def test_missing_required_attributes_are_reported() -> None:
    problems = validate_signal("tool.execute", {"tool": "lookup"})
    assert problems == ("tool.execute: missing required attribute 'generation_id'",)


async def test_a_dead_backend_is_announced_to_its_siblings() -> None:
    from chassis.telemetry import RecordingTelemetry, TeeTelemetry

    class Broken:
        def span(self, name: str, attributes: Any = None) -> Any:
            raise RuntimeError("backend down")

        def event(self, name: str, attributes: Any = None) -> None:
            raise RuntimeError("backend down")

    healthy = RecordingTelemetry()
    tee = TeeTelemetry(Broken(), healthy)

    async with tee.span("harness.reconcile", {"harness": "t"}):
        pass
    tee.event(
        "generation.publish",
        {"generation_id": "gen_0001", "sequence": 1, "previous": None, "plugins": 0},
    )
    await asyncio.sleep(0)

    failures = [event for event in healthy.events if event.name == "telemetry.failure"]
    assert failures, "a contained backend failure must be visible to surviving backends"
    check_recording(healthy)
    assert failures[0].attributes["backend"] == "Broken"
    assert failures[0].attributes["signal"]
