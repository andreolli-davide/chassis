from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from chassis import MODEL, Harness, PluginContext, plugin
from chassis.agents import AgentNotFound, AgentRegistry
from chassis.budget import BudgetDimension, BudgetLimits
from chassis.capabilities import CapabilityKey
from chassis.core.errors import ConfigurationError, HarnessStateError
from chassis.runtime import (
    AgentEvent,
    AgentRequest,
    AgentResult,
    AgentRuntime,
    HarnessRunContext,
)
from chassis.secrets import SecretRedactor
from chassis.testing import fake_tool
from chassis.tools import ToolPolicy


class StubRuntime:
    """Agent runtime that records the run context it was handed."""

    def __init__(self, name: str = "stub") -> None:
        self._name = name
        self.contexts: list[HarnessRunContext] = []
        self.requests: list[AgentRequest] = []

    @property
    def name(self) -> str:
        return self._name

    async def invoke(self, request: AgentRequest, run_context: HarnessRunContext) -> AgentResult:
        self.requests.append(request)
        self.contexts.append(run_context)
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
        self.contexts.append(run_context)
        yield AgentEvent(
            agent=self._name,
            generation_id=run_context.generation_id,
            run_id=run_context.run_id,
            kind="values",
            data=request.input,
        )


def test_stub_runtime_satisfies_the_protocol() -> None:
    assert isinstance(StubRuntime(), AgentRuntime)


async def test_run_context_carries_the_immutable_generation_view() -> None:
    harness = Harness()
    harness.provide(MODEL, {"model": "fake"})
    await harness.start()
    try:
        runtime = StubRuntime()
        harness.register_agent(runtime)

        result = await harness.agents.invoke(
            "stub", {"messages": ["hi"]}, user_id="user-1", tenant_id="tenant-9", thread_id="t-1"
        )

        context = runtime.contexts[0]
        assert context.generation_id == result.generation_id
        assert context.require_capability(MODEL) == {"model": "fake"}
        assert context.user_id == "user-1"
        assert context.tenant_id == "tenant-9"
        assert context.thread_id == "t-1"
        assert dict(context.metadata) == {}
    finally:
        await harness.stop()


async def test_pending_desired_state_is_applied_before_invocation() -> None:
    harness = Harness()
    await harness.start()
    try:
        # Provided after start: the next invocation must observe it.
        harness.provide(MODEL, "late-provider")
        runtime = StubRuntime()
        harness.register_agent(runtime)

        await harness.agents.invoke("stub", {"messages": []})

        assert runtime.contexts[0].require_capability(MODEL) == "late-provider"
        assert harness.has_pending_changes is False
    finally:
        await harness.stop()


async def test_budget_limits_reach_the_run_environment() -> None:
    harness = Harness(default_budget_limits=BudgetLimits(tool_calls=2))
    await harness.start()
    try:
        runtime = StubRuntime()
        harness.register_agent(runtime)
        await harness.agents.invoke("stub", {"messages": []})

        budget = runtime.contexts[0].budget
        assert budget is not None
        assert budget.remaining(BudgetDimension.TOOL_CALLS) == 2
    finally:
        await harness.stop()


async def test_per_call_limits_override_the_default() -> None:
    harness = Harness(default_budget_limits=BudgetLimits(tool_calls=10))
    await harness.start()
    try:
        runtime = StubRuntime()
        harness.register_agent(runtime)
        await harness.agents.invoke("stub", {"messages": []}, limits=BudgetLimits(tool_calls=1))

        budget = runtime.contexts[0].budget
        assert budget is not None
        assert budget.remaining(BudgetDimension.TOOL_CALLS) == 1
    finally:
        await harness.stop()


async def test_environment_is_restricted_to_the_generation() -> None:
    @plugin(name="toolbox-a", version="1.0.0")
    async def toolbox_a(ctx: PluginContext) -> None:
        ctx.tools.register(fake_tool("alpha", result="a"))

    harness = Harness()
    harness.install(toolbox_a, entry_id="tools")
    await harness.start()
    try:
        generation = harness.current_generation
        assert generation is not None
        runtime = StubRuntime()
        harness.register_agent(runtime)
        await harness.agents.invoke("stub", {"messages": []})

        context = runtime.contexts[0]
        assert context.tools is not None
        assert context.tools.names == ("alpha",)
        assert context.environment is not None
        assert context.environment.generation_id == generation.generation_id
    finally:
        await harness.stop()


async def test_registry_rejects_unknown_and_duplicate_agents() -> None:
    harness = Harness()
    harness.register_agent(StubRuntime("one"))

    with pytest.raises(ConfigurationError):
        harness.register_agent(StubRuntime("one"))

    harness.register_agent(StubRuntime("one"), replace=True)

    with pytest.raises(AgentNotFound) as excinfo:
        harness.agents.get("missing")

    assert excinfo.value.context["available"] == ["one"]
    assert harness.agents.names() == ("one",)


async def test_runtime_without_a_name_is_rejected() -> None:
    class Nameless:
        @property
        def name(self) -> str:
            return ""

        async def invoke(
            self, request: AgentRequest, run_context: HarnessRunContext
        ) -> AgentResult:  # pragma: no cover - never reached
            raise NotImplementedError

        async def stream(
            self, request: AgentRequest, run_context: HarnessRunContext
        ) -> AsyncIterator[AgentEvent]:  # pragma: no cover - never reached
            yield AgentEvent(agent="", generation_id="", run_id="", kind="")

    with pytest.raises(ConfigurationError):
        AgentRegistry().register(Nameless())


async def test_invocation_requires_a_harness_bound_registry() -> None:
    registry = AgentRegistry()
    registry.register(StubRuntime())

    with pytest.raises(HarnessStateError):
        await registry.invoke("stub", {"messages": []})


async def test_invocation_is_refused_before_start() -> None:
    harness = Harness()
    harness.register_agent(StubRuntime())

    with pytest.raises(HarnessStateError):
        await harness.agents.invoke("stub", {"messages": []})


async def test_input_and_request_are_mutually_exclusive() -> None:
    harness = Harness()
    harness.register_agent(StubRuntime())
    await harness.start()
    try:
        with pytest.raises(ConfigurationError):
            await harness.agents.invoke("stub", {"a": 1}, request=AgentRequest(input={"b": 2}))
    finally:
        await harness.stop()


async def test_stream_leases_the_generation_for_the_whole_iteration() -> None:
    harness = Harness()
    await harness.start()
    try:
        runtime = StubRuntime()
        harness.register_agent(runtime)

        stream = harness.agents.stream("stub", {"messages": ["hi"]})
        generation_id = None
        async for event in stream:
            generation_id = event.generation_id
            assert harness.current_generation is not None
            assert harness.current_generation.lease_count == 1

        assert generation_id == runtime.contexts[0].generation_id
        assert harness.current_generation.lease_count == 0  # type: ignore[union-attr]
    finally:
        await harness.stop()


async def test_run_metadata_is_carried_verbatim_in_process() -> None:
    """Request metadata reaches the run context unchanged.

    Redaction applies where values leave the process -- traces, snapshots,
    diagnostics -- not to the in-process context a plugin reads.
    """

    harness = Harness()
    await harness.start()
    try:
        runtime = StubRuntime()
        harness.register_agent(runtime)
        await harness.agents.invoke("stub", {"messages": []}, metadata={"request": "abc"})

        assert dict(runtime.contexts[0].metadata) == {"request": "abc"}
    finally:
        await harness.stop()


async def test_provided_capability_is_withdrawn_on_shutdown() -> None:
    harness = Harness()
    harness.provide(MODEL, "value")
    await harness.start()

    assert [item.provider_name for item in harness.capability_registry.registrations()] == [
        "chassis-services"
    ]

    await harness.stop()

    assert harness.capability_registry.registrations() == ()


async def test_withdraw_removes_a_provided_capability() -> None:
    harness = Harness()
    harness.provide(MODEL, "value")
    await harness.start()
    try:
        assert harness.withdraw(MODEL) is True
        await harness.reconcile()
        assert harness.plan().pending == ()
        assert harness.capability_registry.registrations() == ()
        assert harness.withdraw(MODEL) is False
    finally:
        await harness.stop()


async def test_diagnostics_report_boundary_state() -> None:
    @plugin(name="toolbox", version="1.0.0")
    async def toolbox(ctx: PluginContext) -> None:
        ctx.tools.register(fake_tool("alpha", result="a"))

    harness = Harness()
    harness.provide(MODEL, "value")
    harness.install(toolbox, entry_id="toolbox")
    harness.register_agent(StubRuntime())
    await harness.start()
    try:
        status = harness.diagnostics.status()
        assert status["agents"] == 1
        assert status["tools"] == 1
        assert harness.diagnostics.agents()["agents"] == ["stub"]
        assert harness.diagnostics.tools()["tools"][0]["name"] == "alpha"
    finally:
        await harness.stop()


async def test_plugin_diagnostics_list_owned_effects() -> None:
    secret = "sk-live-abcdef123456"

    @plugin(name="owner", version="1.0.0")
    async def owner(ctx: PluginContext) -> None:
        ctx.cleanup("close the pool", lambda: None)
        ctx.cleanup(f"forget {secret}", lambda: None)

    harness = Harness(redactor=SecretRedactor([secret]))
    harness.install(owner, entry_id="owner")
    await harness.start()
    try:
        payload = next(
            item for item in harness.diagnostics.plugins() if item["entry_id"] == "owner"
        )
        effects = payload["effects"]

        assert {effect["description"] for effect in effects} == {
            "close the pool",
            "forget <redacted>",
        }
        assert secret not in str(payload)
        assert all(effect["scope_id"] == payload["scope_id"] for effect in effects)
    finally:
        await harness.stop()


async def test_tool_policy_is_visible_in_diagnostics() -> None:
    @plugin(name="danger", version="1.0.0")
    async def danger(ctx: PluginContext) -> None:
        ctx.tools.register(
            fake_tool("rm", result="deleted"),
            policy=ToolPolicy(permissions=("filesystem.delete",), side_effects=("destructive",)),
        )

    harness = Harness()
    harness.install(danger, entry_id="danger")
    await harness.start()
    try:
        entry = harness.tools.get("rm")
        assert entry is not None
        assert entry.policy.permissions == ("filesystem.delete",)
        assert entry.owner_name == "danger"
        assert entry.scope_id == harness.plugin_registry.instance("danger").scope.id  # type: ignore[union-attr]
    finally:
        await harness.stop()


async def test_capability_key_providers_are_reported() -> None:
    harness = Harness()
    harness.provide(CapabilityKey("database", "1"), "db")
    await harness.start()
    try:
        providers = harness.capability_registry.by_name("database")
        assert len(providers) == 1
        assert providers[0].key == CapabilityKey("database", "1")
    finally:
        await harness.stop()
