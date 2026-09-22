from __future__ import annotations

from typing import Any

import pytest

from chassis import MODEL, Harness
from chassis.core.errors import ConfigurationError
from chassis.evaluation import agent_target, composition_metadata, evaluate_agent
from chassis.runtime import AgentEvent, AgentResult
from chassis.testing import TestHarness

SECRET = "sk-live-abcdef123456"


@pytest.fixture
def harness_with_agent():  # type: ignore[no-untyped-def]
    from tests.test_runtime import StubRuntime

    async def build():  # type: ignore[no-untyped-def]
        harness = TestHarness()
        harness.provide(MODEL, "model")
        harness.register_agent(StubRuntime("stub"))
        await harness.start()
        return harness

    return build


async def test_target_invokes_the_agent_and_reports_attribution(harness_with_agent: Any) -> None:
    harness = await harness_with_agent()
    try:
        target = agent_target(harness, "stub")
        output = await target({"input": {"messages": ["hello"]}})

        assert output["generation_id"] == harness.current_generation.generation_id  # type: ignore[union-attr]
        assert output["snapshot_digest"]
        assert "run_id" in output
    finally:
        await harness.stop()


async def test_target_rejects_a_missing_input_key(harness_with_agent: Any) -> None:
    harness = await harness_with_agent()
    try:
        target = agent_target(harness, "stub", input_key="question")

        with pytest.raises(ConfigurationError) as excinfo:
            await target({"input": "wrong key"})

        assert excinfo.value.context["expected"] == "question"
    finally:
        await harness.stop()


async def test_target_rejects_an_unregistered_agent(harness_with_agent: Any) -> None:
    harness = await harness_with_agent()
    try:
        with pytest.raises(ConfigurationError):
            agent_target(harness, "missing")
    finally:
        await harness.stop()


async def test_target_can_rename_the_output_key(harness_with_agent: Any) -> None:
    harness = await harness_with_agent()
    try:
        target = agent_target(harness, "stub", output_key="answer")
        output = await target({"input": {"messages": []}})

        assert "answer" in output
        assert "output" not in output
    finally:
        await harness.stop()


async def test_composition_metadata_attributes_an_experiment(harness_with_agent: Any) -> None:
    harness = await harness_with_agent()
    try:
        metadata = composition_metadata(harness, agent="stub")

        assert metadata["generation_id"] == harness.current_generation.generation_id  # type: ignore[union-attr]
        assert metadata["chassis_version"]
        assert metadata["agent_runtime"] == "langgraph"
        assert metadata["plugins"] == {"chassis-services": "1.0.0"}
        assert metadata["capabilities"]["model"] == ["1"]
        assert metadata["config_hash"]
        assert metadata["plugin_graph_hash"]
        assert metadata["tool_schema_hash"]
        assert metadata["snapshot_digest"]
    finally:
        await harness.stop()


async def test_composition_metadata_identifies_the_prompt_version(
    harness_with_agent: Any,
) -> None:
    harness = await harness_with_agent()
    try:
        metadata = composition_metadata(harness, agent="stub", prompt_hash="prompt-v7")

        assert metadata["prompt_hash"] == "prompt-v7"
    finally:
        await harness.stop()


async def test_composition_metadata_never_carries_secrets() -> None:
    async with TestHarness() as harness:
        harness.redactor.add(SECRET)
        await harness.start()

        metadata = composition_metadata(harness)

        assert SECRET not in str(metadata)


async def test_composition_metadata_handles_a_harness_without_a_generation() -> None:
    harness = TestHarness()

    metadata = composition_metadata(harness)

    assert metadata["generation_id"] is None

    await harness.stop()


async def test_evaluate_agent_targets_the_dataset_and_labels_the_experiment(
    harness_with_agent: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import langsmith

    captured: dict[str, Any] = {}

    async def fake_evaluate(target: Any, **kwargs: Any) -> dict[str, Any]:
        captured["target"] = target
        captured.update(kwargs)
        return {"experiment": "stub"}

    monkeypatch.setattr(langsmith, "aevaluate", fake_evaluate)

    harness = await harness_with_agent()
    try:
        result = await evaluate_agent(
            harness,
            "stub",
            data=[{"input": {"messages": ["hi"]}}],
            experiment_prefix="chassis-smoke",
        )

        assert result == {"experiment": "stub"}
        assert captured["experiment_prefix"] == "chassis-smoke"
        assert captured["metadata"]["generation_id"] == harness.current_generation.generation_id  # type: ignore[union-attr]

        output = await captured["target"]({"input": {"messages": ["hi"]}})
        assert "generation_id" in output
    finally:
        await harness.stop()


async def test_evaluation_targets_resolve_logical_agent_names() -> None:
    """An AgentSpec's logical name is as targetable as a raw runtime name (R013)."""

    from types import SimpleNamespace

    from chassis.agent_spec import AgentSpec

    class NamedRuntime:
        @property
        def name(self) -> str:
            return "finance-graph"

        async def invoke(self, request, run_context):  # type: ignore[no-untyped-def]
            return AgentResult(
                agent="finance",
                generation_id=run_context.generation_id,
                run_id=run_context.run_id,
                output={"messages": [SimpleNamespace(content="hi")]},
            )

        async def stream(self, request, run_context):  # type: ignore[no-untyped-def]
            yield AgentEvent(
                agent="finance",
                generation_id=run_context.generation_id,
                run_id=run_context.run_id,
                kind="end",
            )

    harness = Harness(name="logical-target")
    harness.register_agent(NamedRuntime())
    harness.agents.install(
        AgentSpec(name="finance", revision="17", runtime_ref="finance-graph", plugins={})
    )
    await harness.start()
    try:
        target = agent_target(harness, "finance")
        output = await target({"input": {"messages": ["hi"]}})
        assert output["output"] == "hi"
    finally:
        await harness.stop()
