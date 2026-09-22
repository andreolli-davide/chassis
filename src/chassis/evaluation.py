"""Making a Chassis agent an evaluation target.

LangSmith is the primary experiment and evaluation platform; Chassis does not
build a competing one. What Chassis adds is attribution: an evaluation run is
labelled with the composition that produced it, so two experiments can be compared
knowing exactly which generation, plugins, tools, and graph they ran against.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from chassis.core.errors import ConfigurationError

if TYPE_CHECKING:
    from chassis.harness import Harness

__all__ = ["agent_target", "composition_metadata", "evaluate_agent"]

#: A LangSmith-compatible evaluation target: dataset example to run output.
EvaluationTarget = Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]]


def _agent_runtime_kind(harness: Harness, agent: str | None) -> str | None:
    """Execution-engine identity of the selected agent runtime, when resolvable.

    The identity comes from the runtime itself — a custom runtime is never
    labelled ``langgraph``.
    """

    if agent is None:
        return None
    from chassis.agents import AgentNotFound

    runtime = None
    try:
        runtime = harness.agents.get(agent)
    except AgentNotFound:
        spec = harness.agents.active_spec(agent)
        if spec is not None and spec.runtime_ref is not None:
            try:
                runtime = harness.agents.get(spec.runtime_ref)
            except AgentNotFound:
                return None
    if runtime is None:
        return None
    kind = getattr(runtime, "runtime_kind", None)
    return kind if isinstance(kind, str) and kind else type(runtime).__name__


def agent_target(
    harness: Harness,
    agent: str,
    *,
    input_key: str = "input",
    output_key: str = "output",
    user_id: str | None = None,
    tenant_id: str | None = None,
) -> EvaluationTarget:
    """Build an async evaluation target that invokes one registered agent.

    The target reads its input from ``example[input_key]`` and returns the agent's
    text under ``output_key``. It reports the generation it ran against so the
    experiment can be attributed even when composition changed mid-experiment.
    """

    if agent not in harness.agents and harness.agents.active_spec(agent) is None:
        raise ConfigurationError("agent is not registered", agent=agent)

    async def target(example: Mapping[str, Any]) -> dict[str, Any]:
        if input_key not in example:
            raise ConfigurationError(
                "evaluation example is missing the input key",
                expected=input_key,
                available=sorted(example),
            )
        result = await harness.agents.invoke(
            agent, example[input_key], user_id=user_id, tenant_id=tenant_id
        )
        return {
            output_key: result.text,
            "generation_id": result.generation_id,
            "run_id": result.run_id,
            "snapshot_digest": result.metadata.get("snapshot_digest"),
        }

    return target


def composition_metadata(
    harness: Harness,
    *,
    agent: str | None = None,
    prompt_hash: str | None = None,
) -> dict[str, Any]:
    """Metadata identifying what an evaluation experiment actually ran.

    Includes generation, plugin versions, capability versions, tool schema, plugin
    graph, and prompt hashes -- the dimensions that make two experiments comparable
    -- and never any configuration values or secrets.
    """

    generation = harness.current_generation
    if generation is None:
        return {"harness": harness.name, "generation_id": None}
    snapshot = harness.snapshot_for(
        generation,
        agent=agent,
        agent_runtime=_agent_runtime_kind(harness, agent),
        prompt_hash=prompt_hash,
    )
    return {
        "harness": harness.name,
        "generation_id": snapshot.generation_id,
        "chassis_version": snapshot.chassis_version,
        "agent_runtime": snapshot.agent_runtime,
        "plugins": dict(snapshot.plugins),
        "capabilities": {name: list(versions) for name, versions in snapshot.capabilities.items()},
        "config_hash": snapshot.config_hash,
        "plugin_graph_hash": snapshot.plugin_graph_hash,
        "tool_schema_hash": snapshot.tool_schema_hash,
        "graph_definition_hash": snapshot.graph_definition_hash,
        "prompt_hash": snapshot.prompt_hash,
        "snapshot_digest": snapshot.digest(),
    }


async def evaluate_agent(
    harness: Harness,
    agent: str,
    *,
    data: Any,
    evaluators: Sequence[Any] = (),
    experiment_prefix: str | None = None,
    input_key: str = "input",
    output_key: str = "output",
    client: Any | None = None,
    **kwargs: Any,
) -> Any:
    """Run a LangSmith experiment over a dataset for one agent.

    The experiment is labelled with :func:`composition_metadata`, so a result can
    be traced back to the exact runtime composition that produced it.
    """

    from chassis._optional import EXTRA_LANGSMITH, require_extra

    require_extra(EXTRA_LANGSMITH, "langsmith", purpose="the LangSmith evaluation helper")
    from langsmith import aevaluate

    target = agent_target(harness, agent, input_key=input_key, output_key=output_key)
    metadata = composition_metadata(harness, agent=agent)
    return await aevaluate(
        target,
        data=data,
        evaluators=list(evaluators),
        experiment_prefix=experiment_prefix or f"chassis:{agent}",
        metadata={**metadata, **kwargs.pop("metadata", {})},
        client=client,
        **kwargs,
    )
