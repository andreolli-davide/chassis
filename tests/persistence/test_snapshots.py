from __future__ import annotations

import pytest

from chassis import MODEL, PluginContext, plugin
from chassis.runtime import HarnessRunContext
from chassis.testing import TestHarness, fake_tool

SECRET = "sk-live-abcdef123456"


def provider_plugin(name: str, capability: str):  # type: ignore[no-untyped-def]
    @plugin(name=name, version="1.0.0", provides={capability: "1.0.0"})
    async def provider(ctx: PluginContext) -> None:
        from chassis.capabilities import CapabilityKey

        ctx.capabilities.provide(CapabilityKey.from_version(capability, "1.0.0"), f"{name}-value")

    return provider


def consumer_plugin() -> type:  # type: ignore[type-arg]
    @plugin(
        name="memory",
        version="2.1.0",
        provides={"memory": "1.0.0"},
        requires={"database": ">=1,<2"},
    )
    async def memory(ctx: PluginContext) -> None:
        from chassis.capabilities import MEMORY

        ctx.require("database")
        ctx.capabilities.provide(MEMORY, "memory-value")

    return memory


async def test_snapshot_describes_the_generation() -> None:
    async with TestHarness() as harness:
        harness.provide(MODEL, "model")
        harness.install_tools(fake_tool("echo", result="x"))
        await harness.reconcile()

        generation = harness.current_generation
        assert generation is not None
        snapshot = harness.snapshot_for(generation, agent="echo-agent")

        assert snapshot.generation_id == generation.generation_id
        assert snapshot.sequence == generation.sequence
        assert snapshot.agent == "echo-agent"
        assert snapshot.chassis_version
        assert snapshot.plugins == {"chassis-services": "1.0.0", "chassis-toolbox": "1.0.0"}
        assert snapshot.capabilities["model"] == ("1",)
        assert snapshot.config_hash
        assert snapshot.plugin_graph_hash
        assert snapshot.tool_schema_hash
        assert snapshot.digest() == snapshot.digest()


async def test_snapshot_digest_changes_with_composition() -> None:
    async with TestHarness() as harness:
        harness.install(provider_plugin("postgres", "database"), entry_id="db")
        await harness.start()
        first = harness.snapshot_for(harness.current_generation)  # type: ignore[arg-type]

        harness.install(consumer_plugin(), entry_id="memory")
        await harness.reconcile()
        second = harness.snapshot_for(harness.current_generation)  # type: ignore[arg-type]

        assert first.digest() != second.digest()
        assert first.plugin_graph_hash != second.plugin_graph_hash
        assert second.plugins == {"postgres": "1.0.0", "memory": "2.1.0"}


async def test_snapshot_digest_is_stable_for_an_unchanged_composition() -> None:
    async with TestHarness() as harness:
        harness.install(provider_plugin("postgres", "database"), entry_id="db")
        await harness.start()
        first = harness.snapshot_for(harness.current_generation)  # type: ignore[arg-type]

        await harness.reconcile()
        second = harness.snapshot_for(harness.current_generation)  # type: ignore[arg-type]

        # A no-op reconcile reuses the generation, so the snapshot is unchanged.
        assert first.to_dict() == second.to_dict()


@plugin(name="configured", version="1.0.0")
async def configured(ctx: PluginContext) -> None:
    return None


async def test_snapshot_contains_no_configuration_values_at_all() -> None:
    async with TestHarness() as harness:
        harness.redactor.add(SECRET)
        harness.install(
            configured,
            entry_id="configured",
            config={"api_key": SECRET, "endpoint": f"https://example.test/{SECRET}", "retries": 3},
        )
        await harness.start()

        snapshot = harness.snapshot_for(harness.current_generation)  # type: ignore[arg-type]
        rendered = str(snapshot.to_dict())

        # A snapshot describes composition; configuration is represented only by
        # its hash, so there is nothing secret to leak in the first place.
        assert SECRET not in rendered
        assert "endpoint" not in rendered
        assert "retries" not in rendered


async def test_config_hash_tracks_configuration_shape_but_not_redacted_values() -> None:
    async with TestHarness() as harness:
        harness.redactor.add(SECRET)
        harness.install(configured, entry_id="a", config={"api_key": SECRET, "retries": 1})
        harness.install(configured, entry_id="b", config={"api_key": SECRET})
        await harness.start()
        first = harness.snapshot_for(harness.current_generation)  # type: ignore[arg-type]

        # Changing only a redacted value leaves the hash stable.
        harness.install(
            configured,
            entry_id="a",
            config={"api_key": "another-secret-value", "retries": 1},
            replace=True,
        )
        await harness.reconcile()
        second = harness.snapshot_for(harness.current_generation)  # type: ignore[arg-type]
        assert first.config_hash == second.config_hash

        # Changing observable configuration does not.
        harness.install(
            configured, entry_id="a", config={"api_key": SECRET, "retries": 2}, replace=True
        )
        await harness.reconcile()
        third = harness.snapshot_for(harness.current_generation)  # type: ignore[arg-type]
        assert third.config_hash != second.config_hash


async def test_snapshot_never_contains_secret_material() -> None:
    async with TestHarness() as harness:
        harness.provide_secret("openai.api_key", SECRET)
        secret = await harness.secrets.get("openai.api_key")
        harness.install(
            provider_plugin("postgres", "database"),
            entry_id="db",
            config={"password": secret.reveal()},
        )
        await harness.start()

        snapshot = harness.snapshot_for(harness.current_generation)  # type: ignore[arg-type]

        assert SECRET not in str(snapshot.to_dict())
        assert SECRET not in str(snapshot.digest())


async def test_snapshot_metadata_is_redacted() -> None:
    async with TestHarness() as harness:
        harness.redactor.add(SECRET)
        await harness.start()

        generation = harness.current_generation
        assert generation is not None
        snapshot = harness.snapshot_for(generation, metadata={"note": f"key={SECRET}"})

        assert SECRET not in str(dict(snapshot.metadata))


async def test_snapshot_from_a_generation_is_immutable() -> None:
    async with TestHarness() as harness:
        await harness.start()
        snapshot = harness.snapshot_for(harness.current_generation)  # type: ignore[arg-type]

        with pytest.raises(AttributeError):
            snapshot.generation_id = "other"  # type: ignore[misc]
        with pytest.raises(TypeError):
            snapshot.metadata["x"] = 1  # type: ignore[index]


async def test_run_snapshot_describes_the_run_generation() -> None:
    from tests.test_runtime import StubRuntime

    async with TestHarness() as harness:
        harness.provide(MODEL, "model")
        harness.register_agent(StubRuntime())
        await harness.agents.invoke("stub", {"messages": []})

        generation = harness.current_generation
        assert generation is not None
        run_context = HarnessRunContext.new(
            generation=generation, environment=harness.run_environment(generation), agent="stub"
        )

        snapshot = harness.run_snapshot(run_context)
        assert snapshot.generation_id == run_context.generation_id
        assert snapshot.agent == "stub"


async def test_run_snapshot_rejects_an_unknown_generation() -> None:
    from chassis.core.errors import HarnessStateError

    async with TestHarness() as harness:
        await harness.start()
        generation = harness.current_generation
        assert generation is not None

        from dataclasses import replace

        stale = replace(generation, generation_id="gen_9999")
        run_context = HarnessRunContext.new(generation=stale)

        with pytest.raises(HarnessStateError):
            harness.run_snapshot(run_context)


async def test_langgraph_runs_are_attributed_to_a_snapshot() -> None:
    from typing import Any

    from langgraph.graph import END, START, StateGraph
    from langgraph.runtime import Runtime
    from typing_extensions import TypedDict

    from chassis.langgraph import AgentDefinition

    class State(TypedDict):
        value: int

    async def node(state: Any, runtime: Runtime[HarnessRunContext]) -> dict[str, Any]:
        assert runtime.context.capabilities.require(MODEL) == "model"
        return {"value": state.get("value", 0) + 1}

    def build(inputs: Any) -> StateGraph[Any, Any, Any, Any]:
        graph: StateGraph[Any, Any, Any, Any] = StateGraph(State, context_schema=HarnessRunContext)
        graph.add_node("node", node)
        graph.add_edge(START, "node")
        graph.add_edge("node", END)
        return graph

    async with TestHarness() as harness:
        harness.provide(MODEL, "model")
        harness.register_agent(
            harness.agent(
                AgentDefinition(name="snapshot-agent", version="1", state_schema=State, build=build)
            )
        )

        result = await harness.agents.invoke("snapshot-agent", {"value": 1})

        assert "snapshot_digest" in result.metadata
        span = harness.telemetry.spans_named("agent.run")[0]
        assert span.attributes["graph_definition_hash"]
        assert span.attributes["snapshot_digest"] == result.metadata["snapshot_digest"]
        assert span.attributes["plugin_graph_hash"]
