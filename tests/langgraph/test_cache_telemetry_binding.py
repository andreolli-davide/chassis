"""The compiled-graph cache shares the agent's telemetry wiring (roadmap R019).

Registering a ``LangGraphAgent`` on a harness binds the harness telemetry into
every signal that agent produces. ``graph.cache`` signals are emitted by the
agent's ``GraphCache``, and a cache left on its ``NoopTelemetry`` default keeps
cache behavior invisible exactly where observability matters. Wiring requested
at construction is a documented override and stays, and without a harness
nothing is ever rebound.
"""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from typing_extensions import TypedDict

from chassis import MODEL
from chassis.langgraph import (
    AgentDefinition,
    GraphBuildInputs,
    GraphCache,
    LangGraphAgent,
    build_cache_key,
)
from chassis.runtime import HarnessRunContext
from chassis.telemetry import RecordingTelemetry
from chassis.testing import FakeChatModel, TestHarness


class ChatState(TypedDict):
    """Graph state: a message list with LangGraph's additive reducer."""

    messages: Annotated[list[AnyMessage], add_messages]


async def model_node(state: Any, runtime: Runtime[HarnessRunContext]) -> dict[str, Any]:
    model = runtime.context.require_capability(MODEL)
    response = await model.ainvoke(state["messages"])
    return {"messages": [response]}


def agent_definition() -> AgentDefinition:
    def build(inputs: GraphBuildInputs) -> StateGraph[Any, Any, Any, Any]:
        graph: StateGraph[Any, Any, Any, Any] = StateGraph(
            ChatState, context_schema=HarnessRunContext
        )
        graph.add_node("model", model_node)
        graph.add_edge(START, "model")
        graph.add_edge("model", END)
        return graph

    return AgentDefinition(name="cache-agent", version="1", state_schema=ChatState, build=build)


async def test_graph_cache_signals_reach_the_harness_once_the_agent_binds() -> None:
    """A defaulted cache must not stay disconnected from bound observability."""

    async with TestHarness() as harness:
        harness.provide(MODEL, FakeChatModel(responses=["one", "two"]))
        # Telemetry defaulted at construction: the cache starts on NoopTelemetry
        # and must adopt the harness's recorder when registration binds services.
        agent = LangGraphAgent(agent_definition())
        harness.register_agent(agent)

        await harness.agents.invoke("cache-agent", {"messages": [HumanMessage("hi")]})
        await harness.agents.invoke("cache-agent", {"messages": [HumanMessage("again")]})

        cache_events = [event for event in harness.telemetry.events if event.name == "graph.cache"]
        assert cache_events, "no graph.cache events reached the harness telemetry"
        results = {str(event.attributes.get("result")) for event in cache_events}
        assert "miss" in results and "hit" in results


async def test_explicitly_wired_cache_telemetry_stays_a_documented_override() -> None:
    """A cache that requested its own backend keeps it when services bind."""

    recorder = RecordingTelemetry()
    async with TestHarness() as harness:
        harness.provide(MODEL, FakeChatModel(responses=["one"]))
        agent = LangGraphAgent(agent_definition(), cache=GraphCache(telemetry=recorder))
        harness.register_agent(agent)

        await harness.agents.invoke("cache-agent", {"messages": [HumanMessage("hi")]})

    assert "graph.cache" in recorder.event_names()
    assert "graph.cache" not in harness.telemetry.event_names()


def test_cache_signals_are_unchanged_without_a_harness() -> None:
    """Without binding, construction-time telemetry keeps receiving cache signals."""

    recorder = RecordingTelemetry()
    cache = GraphCache(telemetry=recorder)
    agent = LangGraphAgent(agent_definition(), cache=cache)
    key = build_cache_key(agent.definition, tools=[], build_time_versions=None)

    assert cache.get(key) is None
    cache.put(key, "graph")  # type: ignore[arg-type]
    assert cache.get(key) is not None

    results = [event.attributes.get("result") for event in recorder.events]
    assert results == ["miss", "hit"]
    assert cache.stats.misses == 1
    assert cache.stats.hits == 1
