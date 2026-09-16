"""Hook boundaries owned by the harness.

Every :class:`HookEvent` member is dispatched somewhere real: lifecycle and
generation events on the control plane, agent events around a run, policy decisions
at the tool authorization boundary, and tool events in the executor (covered by
``tests/tools``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from langchain_core.tools import tool

from chassis import DATABASE, Harness, PluginContext, plugin
from chassis.core.errors import PolicyDenied
from chassis.hooks import HookEvent, HookMode
from chassis.policy import GrantPolicy
from chassis.runtime import AgentEvent, AgentRequest, AgentResult, HarnessRunContext
from chassis.tools import ToolPolicy, ToolRequest

Observation = tuple[HookEvent, dict[str, Any]]


@tool
def echo(text: str) -> str:
    """Return the text unchanged."""

    return text


class StubRuntime:
    """Agent runtime that runs, counts its invocations, or fails on demand."""

    def __init__(self, name: str = "stub", *, error: Exception | None = None) -> None:
        self._name = name
        self._error = error
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def invoke(self, request: AgentRequest, run_context: HarnessRunContext) -> AgentResult:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return AgentResult(
            agent=self._name,
            generation_id=run_context.generation_id,
            run_id=run_context.run_id,
            output={"input": request.input},
            thread_id=request.thread_id,
        )

    async def stream(
        self, request: AgentRequest, run_context: HarnessRunContext
    ) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(
            agent=self._name,
            generation_id=run_context.generation_id,
            run_id=run_context.run_id,
            kind="values",
            data=request.input,
        )


def watcher(seen: list[Observation], *events: HookEvent):  # type: ignore[no-untyped-def]
    """Plugin that records the payload of every event it is asked to watch."""

    @plugin(name="watcher", version="1.0.0")
    async def watch(ctx: PluginContext) -> None:
        def recorder(event: HookEvent):  # type: ignore[no-untyped-def]
            async def handler(payload: Mapping[str, Any]) -> None:
                seen.append((event, dict(payload)))

            return handler

        for event in events:
            ctx.hooks.register(event, recorder(event), mode=HookMode.OBSERVE)

    return watch


def database_plugin():  # type: ignore[no-untyped-def]
    @plugin(name="db", version="1.0.0", provides={"database": "1.0.0"})
    async def db(ctx: PluginContext) -> None:
        ctx.capabilities.provide(DATABASE, "db")

    return db


def events(seen: list[Observation]) -> list[HookEvent]:
    return [event for event, _ in seen]


async def test_lifecycle_events_fire_on_mount_and_unmount() -> None:
    seen: list[Observation] = []
    harness = Harness()
    harness.install(
        watcher(
            seen,
            HookEvent.PLUGIN_MOUNTING,
            HookEvent.PLUGIN_MOUNTED,
            HookEvent.PLUGIN_UNMOUNTING,
            HookEvent.PLUGIN_UNMOUNTED,
        ),
        entry_id="watch",
    )
    await harness.start()
    try:
        # The watcher observes its own mount: its hooks exist by the time the
        # mount completes.
        assert events(seen) == [HookEvent.PLUGIN_MOUNTED]
        assert seen[0][1]["plugin"] == "watcher"
        seen.clear()
        harness.install(database_plugin(), entry_id="db")
        await harness.reconcile()

        assert events(seen) == [HookEvent.PLUGIN_MOUNTING, HookEvent.PLUGIN_MOUNTED]
        assert seen[0][1]["plugin"] == "db"
        assert seen[0][1]["requirements"] == []
        assert seen[1][1]["instance_id"].startswith("plugin_")

        seen.clear()
        instance = harness.plugin_registry.instance("db")
        assert instance is not None
        harness.uninstall("db")
        await harness.reconcile()

        assert events(seen) == [HookEvent.PLUGIN_UNMOUNTING, HookEvent.PLUGIN_UNMOUNTED]
        assert seen[0][1]["scope_id"] == instance.scope.id
        assert seen[1][1]["instance_id"] == instance.instance_id
        assert seen[1][1]["plugin"] == "db"
    finally:
        await harness.stop()


async def test_generation_events_fire_on_publish_and_drain() -> None:
    seen: list[Observation] = []
    harness = Harness()
    harness.install(
        watcher(seen, HookEvent.GENERATION_PUBLISHED, HookEvent.GENERATION_DRAINING),
        entry_id="watch",
    )
    await harness.start()
    try:
        assert events(seen) == [HookEvent.GENERATION_PUBLISHED]
        seen.clear()

        harness.install(database_plugin(), entry_id="db")
        await harness.reconcile()

        assert events(seen) == [HookEvent.GENERATION_DRAINING, HookEvent.GENERATION_PUBLISHED]
        draining, published = seen[0][1], seen[1][1]
        assert draining["successor"] == published["generation_id"]
        assert published["previous"] == draining["generation_id"]
        assert published["plugins"] == 2

        seen.clear()
        await harness.stop()

        assert events(seen) == [HookEvent.GENERATION_DRAINING]
        assert seen[0][1]["shutdown"] is True
    finally:
        await harness.stop()


async def test_agent_run_events_fire_around_a_run() -> None:
    seen: list[Observation] = []
    harness = Harness()
    harness.install(
        watcher(seen, HookEvent.BEFORE_AGENT_RUN, HookEvent.AFTER_AGENT_RUN), entry_id="watch"
    )
    await harness.start()
    try:
        harness.register_agent(StubRuntime())

        result = await harness.agents.invoke("stub", {"messages": []}, thread_id="t-1")

        assert events(seen) == [HookEvent.BEFORE_AGENT_RUN, HookEvent.AFTER_AGENT_RUN]
        assert seen[0][1]["agent"] == "stub"
        assert seen[0][1]["thread_id"] == "t-1"
        assert seen[0][1]["generation_id"] == result.generation_id
        assert seen[1][1]["status"] == "ok"
    finally:
        await harness.stop()


async def test_agent_error_hook_fires_when_the_engine_fails() -> None:
    seen: list[Observation] = []
    harness = Harness()
    harness.install(
        watcher(seen, HookEvent.BEFORE_AGENT_RUN, HookEvent.AGENT_ERROR), entry_id="watch"
    )
    await harness.start()
    try:
        harness.register_agent(StubRuntime(error=RuntimeError("engine exploded")))

        with pytest.raises(RuntimeError):
            await harness.agents.invoke("stub", {"messages": []})

        assert events(seen) == [HookEvent.BEFORE_AGENT_RUN, HookEvent.AGENT_ERROR]
        assert seen[1][1]["error_type"] == "RuntimeError"
        assert "engine exploded" in seen[1][1]["error"]
    finally:
        await harness.stop()


async def test_before_agent_run_hook_can_refuse_the_run() -> None:
    @plugin(name="refuser", version="1.0.0")
    async def refuser(ctx: PluginContext) -> None:
        async def refuse(payload: Mapping[str, Any]) -> bool:
            return True

        ctx.hooks.register(HookEvent.BEFORE_AGENT_RUN, refuse, mode=HookMode.BAIL)

    harness = Harness()
    harness.install(refuser, entry_id="refuser")
    await harness.start()
    try:
        runner = StubRuntime()
        harness.register_agent(runner)

        with pytest.raises(PolicyDenied):
            await harness.agents.invoke("stub", {"messages": []})

        assert runner.calls == 0
    finally:
        await harness.stop()


async def test_policy_decision_hook_fires_for_a_mediated_tool_call() -> None:
    seen: list[Observation] = []
    harness = Harness()
    harness.install(watcher(seen, HookEvent.POLICY_DECISION), entry_id="watch")
    harness.install(
        _tool_plugin(),
        entry_id="fetcher",
    )
    await harness.start()
    try:
        generation = harness.current_generation
        assert generation is not None
        environment = harness.run_environment(generation)
        snapshot = harness.tool_snapshot(generation)

        await environment.executor.execute(
            ToolRequest(name="echo", args={"text": "hi"}),
            snapshot=snapshot,
            policy=GrantPolicy(["network.fetch"]),
            hook_snapshot=environment.hooks,
        )

        # One decision per required permission, then the summary decision.
        assert events(seen) == [HookEvent.POLICY_DECISION, HookEvent.POLICY_DECISION]
        assert seen[0][1]["permission"] == "network.fetch"
        assert seen[0][1]["allowed"] is True
        assert seen[1][1]["tool"] == "echo"
        assert seen[1][1]["allowed"] is True

        seen.clear()
        with pytest.raises(PolicyDenied):
            await environment.executor.execute(
                ToolRequest(name="echo", args={"text": "hi"}),
                snapshot=snapshot,
                policy=GrantPolicy([]),
                hook_snapshot=environment.hooks,
            )

        assert events(seen) == [HookEvent.POLICY_DECISION]
        assert seen[0][1]["allowed"] is False
        assert seen[0][1]["permission"] == "network.fetch"
    finally:
        await harness.stop()


def _tool_plugin():  # type: ignore[no-untyped-def]
    @plugin(name="fetcher", version="1.0.0")
    async def fetcher(ctx: PluginContext) -> None:
        ctx.tools.register(echo, policy=ToolPolicy(permissions=("network.fetch",)))

    return fetcher
