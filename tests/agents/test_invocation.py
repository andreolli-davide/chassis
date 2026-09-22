"""Agent invocation stays pinned to the revision and generation it acquired."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from chassis import Harness, PluginContext, plugin
from chassis.agent_spec import AgentSpec
from chassis.capabilities.keys import CapabilityKey
from chassis.runtime import AgentEvent, AgentRequest, AgentResult, HarnessRunContext

TOOLS = CapabilityKey("tools", "1")


class StubTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = name

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        return input


def toolbox(name: str, *tool_names: str):  # type: ignore[no-untyped-def]
    @plugin(name=name, version="1.0.0", provides={"tools": "1.0.0"})
    async def box(ctx: PluginContext) -> None:
        for tool_name in tool_names:
            ctx.tools.register(StubTool(tool_name))
        ctx.capabilities.provide(TOOLS, tuple(tool_names))

    return box


class RecordingRuntime:
    """A minimal runtime that records the context each run acquired."""

    name = "finance-graph"

    def __init__(self) -> None:
        self.contexts: list[HarnessRunContext] = []
        self.started: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def invoke(self, request: AgentRequest, run_context: HarnessRunContext) -> AgentResult:
        self.contexts.append(run_context)
        if self.started is not None:
            self.started.set()
            assert self.release is not None
            await self.release.wait()
        return AgentResult(
            agent=self.name,
            generation_id=run_context.generation_id,
            run_id=run_context.run_id,
            output={"messages": []},
        )

    async def stream(
        self, request: AgentRequest, run_context: HarnessRunContext
    ) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(
            agent=self.name,
            generation_id=run_context.generation_id,
            run_id=run_context.run_id,
            kind="values",
            agent_revision=run_context.agent_revision,
        )


def harness() -> Harness:
    h = Harness(name="agents")
    h.register_plugin_type("ledger", ledger())
    return h


def ledger():  # type: ignore[no-untyped-def]
    @plugin(name="ledger", version="1.0.0", provides={"database": "1.0.0"})
    async def provide(ctx: PluginContext) -> None:
        ctx.capabilities.provide(CapabilityKey("database", "1"), "ledger")

    return provide


async def test_a_run_is_attributed_to_its_agent_revision() -> None:
    h = harness()
    runtime = RecordingRuntime()
    h.register_agent(runtime)
    h.agents.install(
        AgentSpec(
            name="finance",
            revision="17",
            runtime_ref="finance-graph",
            plugins={"ledger": {}},
        )
    )
    try:
        result = await h.agents.invoke("finance", {"messages": []})

        assert result.agent == "finance"
        assert result.agent_revision == "17"
        context = runtime.contexts[0]
        assert context.agent == "finance"
        assert context.agent_revision == "17"
        assert context.agent_identity == "finance@17"
        assert context.to_dict()["agent_revision"] == "17"

        snapshot = h.run_snapshot(context)
        assert snapshot.agent == "finance"
        assert snapshot.agent_revision == "17"
        assert snapshot.agent_identity == "finance@17"
        assert snapshot.to_dict()["agent_revision"] == "17"
    finally:
        await h.stop()


async def test_a_run_keeps_its_revision_when_a_new_one_is_published() -> None:
    h = harness()
    runtime = RecordingRuntime()
    runtime.started = asyncio.Event()
    runtime.release = asyncio.Event()
    h.register_agent(runtime)
    h.agents.install(
        AgentSpec(
            name="finance", revision="17", runtime_ref="finance-graph", plugins={"ledger": {}}
        )
    )
    try:
        await h.start()
        generation_17 = h.current_generation
        assert generation_17 is not None

        run_a = asyncio.create_task(h.agents.invoke("finance", {"messages": []}))
        await runtime.started.wait()

        h.agents.replace(
            AgentSpec(
                name="finance",
                revision="18",
                runtime_ref="finance-graph",
                plugins={"ledger": {}},
            )
        )
        await h.reconcile()
        generation_18 = h.current_generation
        assert generation_18 is not None and generation_18 is not generation_17

        runtime.started = None
        runtime.release.set()
        first = await run_a
        second = await h.agents.invoke("finance", {"messages": []})

        assert first.generation_id == generation_17.generation_id
        assert first.agent_revision == "17"
        assert second.generation_id == generation_18.generation_id
        assert second.agent_revision == "18"
        assert runtime.contexts[0].agent_revision == "17"
        assert runtime.contexts[1].agent_revision == "18"
    finally:
        await h.stop()


async def test_streamed_events_carry_the_pinned_revision() -> None:
    h = harness()
    runtime = RecordingRuntime()
    h.register_agent(runtime)
    h.agents.install(
        AgentSpec(
            name="finance", revision="17", runtime_ref="finance-graph", plugins={"ledger": {}}
        )
    )
    try:
        events = [event async for event in h.agents.stream("finance", {"messages": []})]

        assert len(events) == 1
        assert events[0].agent_revision == "17"
        assert events[0].to_dict()["agent_revision"] == "17"
    finally:
        await h.stop()


async def test_the_run_environment_is_narrowed_to_the_agent_scope() -> None:
    h = Harness(name="agents")
    h.register_plugin_type("tools", toolbox("tools", "search", "notes"))
    h.register_agent(RecordingRuntime())
    h.agents.install(
        AgentSpec(
            name="finance",
            revision="17",
            runtime_ref="finance-graph",
            tools=["search"],
            plugins={"tools": {}},
        )
    )
    try:
        await h.agents.invoke("finance", {"messages": []})

        generation = h.current_generation
        assert generation is not None
        assert h.run_environment(generation, scope="/agents/finance").tools.names == ("search",)
    finally:
        await h.stop()


async def test_a_runtime_only_agent_still_invokes_without_a_revision() -> None:
    h = harness()
    runtime = RecordingRuntime()
    h.register_agent(runtime)
    try:
        await h.start()
        result = await h.agents.invoke("finance-graph", {"messages": []})

        assert result.agent_revision is None
        assert runtime.contexts[0].agent_revision is None
        assert runtime.contexts[0].agent_identity == "finance-graph"
    finally:
        await h.stop()


async def test_agents_installed_after_start_resolve_and_run() -> None:
    """Readiness reconciliation runs before agent lookup (R013)."""

    def runtime_owner(runtime):  # type: ignore[no-untyped-def]
        @plugin(name="runtime-owner", version="1.0.0")
        async def provide(ctx: PluginContext) -> None:
            ctx.agents.register(runtime, replace=True)

        return provide

    harness = Harness(name="late-agent")
    harness.register_plugin_type("runtime-owner", runtime_owner(RecordingRuntime()))
    await harness.start()
    try:
        harness.agents.install(
            AgentSpec(
                name="finance",
                revision="1",
                runtime_ref="finance-graph",
                plugins={"runtime-owner": {}},
            )
        )
        result = await harness.agents.invoke("finance", {"messages": []})
        assert result.agent_revision == "1"
    finally:
        await harness.stop()


async def test_agents_replaced_after_start_resolve_and_run() -> None:
    def runtime_owner(runtime):  # type: ignore[no-untyped-def]
        @plugin(name="runtime-owner", version="1.0.0")
        async def provide(ctx: PluginContext) -> None:
            ctx.agents.register(runtime, replace=True)

        return provide

    harness = Harness(name="replaced-agent")
    harness.register_plugin_type("runtime-owner", runtime_owner(RecordingRuntime()))
    harness.register_agent(RecordingRuntime())
    harness.agents.install(
        AgentSpec(name="finance", revision="1", runtime_ref="finance-graph", plugins={})
    )
    await harness.start()
    try:
        harness.agents.replace(
            AgentSpec(
                name="finance",
                revision="2",
                runtime_ref="finance-graph",
                plugins={"runtime-owner": {}},
            )
        )
        result = await harness.agents.invoke("finance", {"messages": []})
        assert result.agent_revision == "2"
    finally:
        await harness.stop()


async def test_convenience_arguments_are_rejected_alongside_a_complete_request() -> None:
    from chassis.core.errors import ConfigurationError
    from chassis.runtime import AgentRequest

    h = harness()
    h.register_agent(RecordingRuntime())
    await h.start()
    try:
        request = AgentRequest(input={"messages": []}, thread_id="t1")
        for kwargs in (
            {"thread_id": "t2"},
            {"resume": "r"},
            {"checkpoint_id": "c"},
            {"metadata": {"m": 1}},
        ):
            with pytest.raises(ConfigurationError) as excinfo:
                await h.agents.invoke("finance-graph", request=request, **kwargs)  # type: ignore[arg-type]
            assert excinfo.value.context["ignored"]
    finally:
        await h.stop()
