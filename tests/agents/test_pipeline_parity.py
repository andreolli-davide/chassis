"""Invoke and stream share one run lifecycle (roadmap R014).

Parity across both paths for attribution, hook failures, nested budgets,
cancellation, and telemetry: the same `agent.run` span and snapshot digest, the
same hook ordering and errors, the parent budget active through before/after
hooks, and every streamed event stamped with the run's attribution.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from chassis import Harness, PluginContext, plugin
from chassis.budget.governor import current_budget
from chassis.budget.models import BudgetLimits
from chassis.core.errors import AgentExecutionError, PolicyDenied
from chassis.hooks import HookEvent, HookMode
from chassis.runtime import AgentEvent, AgentRequest, AgentResult, HarnessRunContext
from chassis.telemetry import RecordingTelemetry


class ParityRuntime:
    """Runtime that records its drives and can fail or block on demand."""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return "parity-graph"

    async def invoke(self, request: AgentRequest, run_context: HarnessRunContext) -> AgentResult:
        self.calls.append("invoke")
        if self.error is not None:
            raise self.error
        return AgentResult(
            agent="parity",
            generation_id=run_context.generation_id,
            run_id=run_context.run_id,
            thread_id=request.thread_id,
            output={"messages": [SimpleNamespace(content="hi")]},
        )

    async def stream(
        self, request: AgentRequest, run_context: HarnessRunContext
    ) -> AsyncIterator[AgentEvent]:
        self.calls.append("stream")
        if self.error is not None:
            raise self.error
        yield AgentEvent(
            agent="wrong",
            generation_id="wrong",
            run_id="wrong",
            kind="message",
            data="hi",
        )


def blocking_runtime(entered, release):  # type: ignore[no-untyped-def]
    class Blocking(ParityRuntime):
        async def invoke(self, request, run_context):  # type: ignore[no-untyped-def]
            self.calls.append("invoke")
            entered.set()
            await release.wait()
            return await super().invoke(request, run_context)

        async def stream(self, request, run_context):  # type: ignore[no-untyped-def]
            self.calls.append("stream")
            entered.set()
            await release.wait()
            async for event in super().stream(request, run_context):  # type: ignore[misc]
                yield event

    return Blocking()


def build(runtime: ParityRuntime, telemetry: RecordingTelemetry, hooks: list[Mapping[str, Any]]):  # type: ignore[no-untyped-def]
    harness = Harness(name="parity", telemetry=telemetry)
    harness.register_agent(runtime)

    async def watcher(payload: Mapping[str, Any]) -> None:
        hooks.append(payload)

    @plugin(name="watch", version="1.0.0")
    async def watch(ctx: PluginContext) -> None:
        ctx.hooks.register(
            HookEvent.BEFORE_AGENT_RUN,
            watcher,
        )
        ctx.hooks.register(HookEvent.AFTER_AGENT_RUN, watcher)
        ctx.hooks.register(HookEvent.AGENT_ERROR, watcher)

    harness.install(watch, entry_id="watch")
    return harness


async def test_invoke_and_stream_emit_the_same_run_span_and_attribution() -> None:
    runtime = ParityRuntime()
    telemetry = RecordingTelemetry()
    harness = build(runtime, telemetry, [])
    await harness.start()
    try:
        result = await harness.agents.invoke("parity-graph", {"messages": []}, thread_id="t1")
        invoke_span = telemetry.spans_named("agent.run")[0]
        telemetry.clear()

        events = [
            event
            async for event in harness.agents.stream(
                "parity-graph", {"messages": []}, thread_id="t1"
            )
        ]
        stream_span = telemetry.spans_named("agent.run")[0]

        # The same span and snapshot digest for both paths.
        assert invoke_span.attributes == stream_span.attributes
        assert result.metadata["snapshot_digest"] == stream_span.attributes["snapshot_digest"]

        # Every streamed event is stamped with the run's attribution, never
        # trusting the runtime's own fields.
        assert events
        for event in events:
            assert event.agent == "parity-graph"
            assert event.agent_revision is None
            assert event.generation_id == result.generation_id
            assert event.thread_id == "t1"
            assert event.run_id  # unique per run, present on every event
        assert result.thread_id == "t1"
    finally:
        await harness.stop()


async def test_hook_failures_are_identical_for_invoke_and_stream() -> None:
    seen: list[Mapping[str, Any]] = []
    runtime = ParityRuntime(error=RuntimeError("boom"))
    harness = build(runtime, RecordingTelemetry(), seen)

    @plugin(name="refuser", version="1.0.0")
    async def refuser(ctx: PluginContext) -> None:
        async def refuse(payload: Mapping[str, Any]) -> bool:
            return True

        ctx.hooks.register(HookEvent.BEFORE_AGENT_RUN, refuse, mode=HookMode.BAIL)

    harness.install(refuser, entry_id="refuser")
    await harness.start()
    try:
        with pytest.raises(PolicyDenied) as invoke_error:
            await harness.agents.invoke("parity-graph", {"messages": []})

        with pytest.raises(PolicyDenied) as stream_error:
            async for _ in harness.agents.stream("parity-graph", {"messages": []}):
                pass

        assert type(invoke_error.value) is type(stream_error.value)
    finally:
        await harness.stop()


async def test_agent_errors_are_identical_for_invoke_and_stream() -> None:
    seen: list[Mapping[str, Any]] = []
    runtime = ParityRuntime(error=RuntimeError("boom"))
    harness = build(runtime, RecordingTelemetry(), seen)
    await harness.start()
    try:
        with pytest.raises(AgentExecutionError) as invoke_error:
            await harness.agents.invoke("parity-graph", {"messages": []})

        with pytest.raises(AgentExecutionError) as stream_error:
            async for _ in harness.agents.stream("parity-graph", {"messages": []}):
                pass

        assert type(invoke_error.value) is type(stream_error.value)
        assert isinstance(invoke_error.value.__cause__, RuntimeError)
        assert isinstance(stream_error.value.__cause__, RuntimeError)

        # The error hook fired on both paths with the same payload shape.
        error_payloads = [payload for payload in seen if "error" in payload]
        assert len(error_payloads) == 2
        assert error_payloads[0].keys() == error_payloads[1].keys()
    finally:
        await harness.stop()


async def test_parent_budget_is_active_through_hooks_on_both_paths() -> None:
    seen: list[str] = []
    runtime = ParityRuntime()

    async def budget_watcher(payload: Mapping[str, Any]) -> None:
        seen.append("budgeted" if current_budget() is not None else "unbudgeted")

    harness = Harness(name="parity-budget")
    harness.register_agent(runtime)

    @plugin(name="watch", version="1.0.0")
    async def watch(ctx: PluginContext) -> None:
        ctx.hooks.register(HookEvent.BEFORE_AGENT_RUN, budget_watcher)
        ctx.hooks.register(HookEvent.AFTER_AGENT_RUN, budget_watcher)

    harness.install(watch, entry_id="watch")
    await harness.start()
    try:
        limits = BudgetLimits(tokens=100)
        await harness.agents.invoke("parity-graph", {"messages": []}, limits=limits)
        async for _ in harness.agents.stream("parity-graph", {"messages": []}, limits=limits):
            pass

        # before + after for invoke, before + after for stream: all budgeted.
        assert seen == ["budgeted"] * 4
    finally:
        await harness.stop()


async def test_cancellation_releases_both_paths_identically() -> None:
    import asyncio

    entered_invoke = asyncio.Event()
    release_invoke = asyncio.Event()
    runtime_invoke = blocking_runtime(entered_invoke, release_invoke)
    harness_invoke = Harness(name="cancel-invoke")
    harness_invoke.register_agent(runtime_invoke)
    await harness_invoke.start()
    try:
        task = asyncio.create_task(harness_invoke.agents.invoke("parity-graph", {"messages": []}))
        await entered_invoke.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert harness_invoke.generation_manager.current is not None
        assert harness_invoke.generation_manager.current.lease_count == 0
    finally:
        release_invoke.set()
        await harness_invoke.stop()

    entered_stream = asyncio.Event()
    release_stream = asyncio.Event()
    runtime_stream = blocking_runtime(entered_stream, release_stream)
    harness_stream = Harness(name="cancel-stream")
    harness_stream.register_agent(runtime_stream)
    await harness_stream.start()
    try:
        stream = harness_stream.agents.stream("parity-graph", {"messages": []})
        task = asyncio.create_task(anext(stream))  # type: ignore[call-overload]
        await entered_stream.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert harness_stream.generation_manager.current is not None
        assert harness_stream.generation_manager.current.lease_count == 0
    finally:
        release_stream.set()
        await harness_stream.stop()
