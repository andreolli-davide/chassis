"""Published state is deeply immutable (roadmap R009).

Recursive copy-and-freeze applies at every publication boundary: nested
mutation of any published container must raise, and author-owned dicts must
never alias published state. The intentional exception: executable provider
and tool objects are live runtime objects — their contracts and metadata are
frozen, not their internals.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.tools import tool

from chassis import Harness, PluginContext, plugin
from chassis.capabilities import DATABASE
from chassis.capabilities.snapshot import CapabilitySnapshot
from chassis.core.generations import GenerationManager
from chassis.runtime import AgentRequest, AgentResult, HarnessRunContext
from chassis.tools import ToolPolicy

NESTED: dict[str, Any] = {"a": {"b": {"c": 1}}, "rows": [{"d": 2}]}


def assert_deeply_frozen(container: Any) -> None:
    with pytest.raises(TypeError):
        container["a"]["b"] = 9  # type: ignore[index]
    with pytest.raises(TypeError):
        container["a"]["b"]["c"] = 9  # type: ignore[index]
    with pytest.raises(TypeError):
        container["rows"][0]["d"] = 9  # type: ignore[index]


def empty_snapshot(generation_id: str) -> CapabilitySnapshot:
    return CapabilitySnapshot(generation_id=generation_id)


@tool
def echo(text: str) -> str:
    """Echo the input."""

    return text


def provider():  # type: ignore[no-untyped-def]
    @plugin(
        name="meta",
        version="1.0.0",
        provides={"database": "1.0.0"},
        metadata={"note": dict(NESTED)},
    )
    async def provide(ctx: PluginContext) -> None:
        ctx.capabilities.provide(DATABASE, "db", metadata={"note": dict(NESTED)})
        ctx.tools.register(
            echo,
            policy=ToolPolicy(metadata={"note": dict(NESTED)}),
            metadata={"note": dict(NESTED)},
        )

    return provide


async def test_plugin_config_and_manifest_metadata_are_deeply_frozen() -> None:
    harness = Harness()
    source = dict(NESTED)
    harness.install(provider(), entry_id="db", config={"note": source})
    await harness.start()
    try:
        entry = harness.entry("db")
        assert entry is not None
        assert_deeply_frozen(entry.config["note"])
        instance = harness.plugin_registry.instance("db")
        assert instance is not None
        assert_deeply_frozen(instance.config["note"])
        assert_deeply_frozen(entry.manifest.metadata["note"])

        # The author's container can change freely; published state cannot.
        source["a"]["b"]["c"] = 42  # type: ignore[index]
        assert instance.config["note"]["a"]["b"]["c"] == 1  # type: ignore[index]
    finally:
        await harness.stop()


async def test_scope_and_resolved_scope_containers_are_deeply_frozen() -> None:
    harness = Harness()
    harness.composition.child("research", metadata={"note": dict(NESTED)})
    harness.install(provider(), entry_id="db")
    await harness.start()
    try:
        generation = harness.current_generation
        assert generation is not None
        resolved = generation.scopes.get("/research")
        assert resolved is not None
        assert_deeply_frozen(resolved.metadata["note"])
        with pytest.raises(TypeError):
            resolved.visible["database"] = ()  # type: ignore[index]
    finally:
        await harness.stop()


async def test_registration_and_snapshot_metadata_are_deeply_frozen() -> None:
    harness = Harness()
    harness.install(provider(), entry_id="db")
    await harness.start()
    try:
        generation = harness.current_generation
        assert generation is not None

        registration = harness.capability_registry.registrations()[0]
        assert_deeply_frozen(registration.metadata["note"])

        tool_entry = harness.tools.entries()[0]
        assert_deeply_frozen(tool_entry.metadata["note"])
        assert_deeply_frozen(tool_entry.policy.metadata["note"])

        snapshot = harness.snapshot_for(generation, metadata={"note": dict(NESTED)})
        assert_deeply_frozen(snapshot.metadata["note"])
    finally:
        await harness.stop()


def test_run_metadata_is_deeply_frozen() -> None:
    assert_deeply_frozen(AgentRequest(metadata={"note": dict(NESTED)}).metadata["note"])
    result = AgentResult(agent="a", generation_id="g", run_id="r", metadata={"note": dict(NESTED)})
    assert_deeply_frozen(result.metadata["note"])

    manager = GenerationManager()
    generation = manager.build(snapshot_factory=empty_snapshot, instances=[])
    context = HarnessRunContext.new(generation=generation, metadata={"note": dict(NESTED)})
    assert_deeply_frozen(context.metadata["note"])


def test_generation_metadata_is_deeply_frozen() -> None:
    manager = GenerationManager()
    generation = manager.build(
        snapshot_factory=empty_snapshot,
        instances=[],
        metadata={"note": dict(NESTED)},
    )
    assert_deeply_frozen(generation.metadata["note"])
