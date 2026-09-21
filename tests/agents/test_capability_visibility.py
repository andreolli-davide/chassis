"""AgentSpec composition visibility at runtime (roadmap R008).

The acquired ``ResolvedScope.visible`` registrations are enforced for
``HarnessRunContext.require_capability()`` — hidden, inherited, narrowed,
sibling-local, and versioned capabilities resolve exactly as composed, in both
the invoke and the stream path. Composition visibility is isolation of
composition, not a security sandbox.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from chassis import Harness, PluginContext, plugin
from chassis.agent_spec import AgentSpec
from chassis.capabilities import CapabilityKey
from chassis.core.errors import CapabilityNotFound, CapabilityVersionMismatch, ChassisError
from chassis.runtime import AgentEvent, AgentRequest, AgentResult, HarnessRunContext

DATABASE_V1 = CapabilityKey("database", "1")
DATABASE_V2 = CapabilityKey("database", "2")
MEMORY = CapabilityKey("memory", "1")
ERP = CapabilityKey("erp", "1")


class ProbeRuntime:
    """Runtime that requires one capability in both execution paths."""

    def __init__(self, key: CapabilityKey) -> None:
        self.key = key
        self.outcomes: list[tuple[str, Any]] = []

    @property
    def name(self) -> str:
        return "probe-graph"

    def _probe(self, path: str, run_context: HarnessRunContext) -> None:
        try:
            self.outcomes.append((path, run_context.require_capability(self.key)))
        except ChassisError as error:
            self.outcomes.append((path, error))

    async def invoke(self, request: AgentRequest, run_context: HarnessRunContext) -> AgentResult:
        self._probe("invoke", run_context)
        return AgentResult(
            agent="probe",
            generation_id=run_context.generation_id,
            run_id=run_context.run_id,
        )

    async def stream(
        self, request: AgentRequest, run_context: HarnessRunContext
    ) -> AsyncIterator[AgentEvent]:
        self._probe("stream", run_context)
        yield AgentEvent(
            agent="probe",
            generation_id=run_context.generation_id,
            run_id=run_context.run_id,
            kind="end",
        )


def provider(name: str, capability: CapabilityKey, value: str, version: str):  # type: ignore[no-untyped-def]
    @plugin(name=name, version="1.0.0", provides={capability.name: version})
    async def provide(ctx: PluginContext) -> None:
        ctx.capabilities.provide(capability, value, version=version)

    return provide


async def probe_both_paths(
    harness: Harness,
    key: CapabilityKey,
    *,
    capabilities: frozenset[str] | None,
    plugins: dict[str, dict[str, Any]] | None = None,
) -> list[tuple[str, Any]]:
    runtime = ProbeRuntime(key)
    harness.register_agent(runtime)
    harness.agents.install(
        AgentSpec(
            name="probe",
            revision="1",
            runtime_ref="probe-graph",
            capabilities=capabilities,
            plugins=plugins or {},
        )
    )
    await harness.start()
    try:
        await harness.agents.invoke("probe", {"messages": []})
        async for _ in harness.agents.stream("probe", {"messages": []}):
            pass
    finally:
        await harness.stop()
    return runtime.outcomes


async def test_hidden_capabilities_fail_in_invoke_and_stream() -> None:
    harness = Harness()
    harness.install(provider("db", DATABASE_V1, "root-db", "1.0.0"), entry_id="db")
    harness.install(provider("mem", MEMORY, "root-mem", "1.0.0"), entry_id="mem")

    outcomes = await probe_both_paths(harness, MEMORY, capabilities=frozenset({"database"}))

    assert [path for path, _ in outcomes] == ["invoke", "stream"]
    for _path, outcome in outcomes:
        assert isinstance(outcome, CapabilityNotFound)


async def test_inherited_capabilities_stay_available() -> None:
    harness = Harness()
    harness.install(provider("db", DATABASE_V1, "root-db", "1.0.0"), entry_id="db")
    harness.install(provider("mem", MEMORY, "root-mem", "1.0.0"), entry_id="mem")

    outcomes = await probe_both_paths(harness, DATABASE_V1, capabilities=frozenset({"database"}))

    assert outcomes == [("invoke", "root-db"), ("stream", "root-db")]


async def test_a_local_capability_outside_the_view_is_still_hidden() -> None:
    harness = Harness()
    harness.install(provider("db", DATABASE_V1, "root-db", "1.0.0"), entry_id="db")
    harness.register_plugin_type("local", provider("mem", MEMORY, "local-mem", "1.0.0"))

    outcomes = await probe_both_paths(
        harness, MEMORY, capabilities=frozenset({"database"}), plugins={"local": {}}
    )

    # The view is a ceiling: a provider inside the agent scope is hidden too.
    for _path, outcome in outcomes:
        assert isinstance(outcome, CapabilityNotFound)


async def test_sibling_local_capabilities_are_not_visible() -> None:
    harness = Harness()
    harness.install(provider("db", DATABASE_V1, "root-db", "1.0.0"), entry_id="db")
    harness.composition.child("finance")
    harness.install(provider("erp", ERP, "finance-erp", "1.0.0"), entry_id="erp", scope="/finance")

    outcomes = await probe_both_paths(harness, ERP, capabilities=None)

    for _path, outcome in outcomes:
        assert isinstance(outcome, CapabilityNotFound)


async def test_versioned_contracts_resolve_against_the_scoped_snapshot() -> None:
    harness = Harness()
    harness.composition.child("finance")
    harness.install(
        provider("finance-db", DATABASE_V1, "finance-db1", "1.0.0"),
        entry_id="finance-db",
        scope="/finance",
    )
    harness.register_plugin_type("local", provider("local-db", DATABASE_V2, "probe-db2", "2.0.0"))

    # The hidden sibling's @1 must not satisfy the probe; the scoped snapshot
    # knows only the visible @2 registration.
    hidden = await probe_both_paths(
        harness, DATABASE_V1, capabilities=frozenset({"database"}), plugins={"local": {}}
    )
    for _path, outcome in hidden:
        assert isinstance(outcome, (CapabilityNotFound, CapabilityVersionMismatch))


async def test_versioned_contracts_bind_the_visible_generation() -> None:
    harness = Harness()
    harness.composition.child("finance")
    harness.install(
        provider("finance-db", DATABASE_V1, "finance-db1", "1.0.0"),
        entry_id="finance-db",
        scope="/finance",
    )
    harness.register_plugin_type("local", provider("local-db", DATABASE_V2, "probe-db2", "2.0.0"))

    outcomes = await probe_both_paths(
        harness, DATABASE_V2, capabilities=frozenset({"database"}), plugins={"local": {}}
    )

    assert outcomes == [("invoke", "probe-db2"), ("stream", "probe-db2")]


async def test_unknown_scope_paths_are_rejected_not_silently_empty() -> None:
    harness = Harness()
    harness.install(provider("db", DATABASE_V1, "root-db", "1.0.0"), entry_id="db")
    await harness.start()
    try:
        generation = harness.current_generation
        assert generation is not None

        from chassis.core.errors import ConfigurationError

        with pytest.raises(ConfigurationError):
            harness.tool_snapshot(generation, scope="/does-not-exist")
        with pytest.raises(ConfigurationError):
            harness.run_environment(generation, scope="/does-not-exist")
    finally:
        await harness.stop()
