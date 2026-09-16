"""Quickstart --- the smallest useful Chassis application.

Everything else in ``examples/`` adds one concept at a time; this file is the
starting point: install Chassis, give it a model, run a LangGraph agent, and read
the result with the generation it ran against.

Run it with::

    uv run python examples/quickstart.py

The script asserts what it prints, so running it verifies the claims.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from typing_extensions import TypedDict

from chassis import MODEL, Harness
from chassis.langgraph import AgentDefinition, GraphBuildInputs, LangGraphAgent
from chassis.runtime import HarnessRunContext
from chassis.testing import FakeChatModel


class ChatState(TypedDict):
    """Conversation state: LangGraph owns it, Chassis never touches it."""

    messages: Annotated[list[AnyMessage], add_messages]


async def assistant(state: ChatState, runtime: Runtime[HarnessRunContext]) -> dict[str, Any]:
    """One node, reading the model from the run's own immutable generation."""

    model = runtime.context.require_capability(MODEL)
    return {"messages": [await model.ainvoke(state["messages"])]}


def build_agent(inputs: GraphBuildInputs) -> StateGraph[Any, Any, Any, Any]:
    """The graph: a single model call. Nothing here knows what Chassis is."""

    graph: StateGraph[Any, Any, Any, Any] = StateGraph(ChatState, context_schema=HarnessRunContext)
    graph.add_node("assistant", assistant)
    graph.add_edge(START, "assistant")
    graph.add_edge("assistant", END)
    return graph


async def main() -> None:
    harness = Harness(name="quickstart")

    # Capabilities are provided under a key; nothing resolves "the current model".
    harness.provide(MODEL, FakeChatModel(responses=["hello from Chassis"]))

    # An agent is a runtime plus a definition; the harness owns its lifecycle.
    harness.register_agent(
        LangGraphAgent(
            AgentDefinition(
                name="research-agent",
                version="1",
                state_schema=ChatState,
                build=build_agent,
            ),
            checkpointer=InMemorySaver(),
        )
    )

    async with harness:
        result = await harness.agents.invoke(
            "research-agent",
            {"messages": [HumanMessage("hi")]},
            thread_id="thread-1",
        )

        print(f"[invoke] text={result.text!r}")
        print(f"[invoke] generation={result.generation_id} thread={result.thread_id}")
        print(f"[invoke] snapshot={result.metadata['snapshot_digest']}")

        assert result.text == "hello from Chassis"
        assert result.generation_id == "gen_0001"
        assert result.metadata["snapshot_digest"]

        status = harness.diagnostics.status()
        print(f"[status] {status['plugins']} agents={status['agents']}")

        assert status["state"] == "running"
        assert status["agents"] == 1

    print("\nQuickstart completed: model capability, LangGraph agent, generation")
    print("attribution, and diagnostics all verified.")


if __name__ == "__main__":
    asyncio.run(main())
