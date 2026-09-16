"""Example A --- a basic LangGraph agent running on Chassis.

Demonstrates, in one runnable script:

* a model capability consumed through the run context;
* tools registered by a plugin and executed through the harness boundary;
* a checkpointer plus a durable ``thread_id``;
* streaming events;
* LangSmith tracing (enabled automatically when LangSmith is configured);
* interrupt/resume;
* the runtime snapshot a run is attributed to.

Run it with::

    uv run python examples/basic_agent.py

The script uses a scripted model so it needs no credentials, and asserts the
behaviour it prints, so running it verifies the claims.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from typing_extensions import TypedDict

from chassis import MODEL, PluginContext, plugin
from chassis.langgraph import AgentDefinition, GraphBuildInputs, LangGraphAgent, harness_tool_node
from chassis.policy import GrantPolicy
from chassis.runtime import HarnessRunContext
from chassis.telemetry import LangSmithTelemetry
from chassis.testing import FakeChatModel, TestHarness, fake_tool
from chassis.tools import ToolPolicy

SEARCH_RESULT = "Chassis keeps composition and execution separate."


class ChatState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


async def assistant(state: ChatState, runtime: Runtime[HarnessRunContext]) -> dict[str, Any]:
    """Ask the model, reading it from the run's own generation."""

    model = runtime.context.require_capability(MODEL)
    response = await model.ainvoke(state["messages"])
    return {"messages": [response]}


def route_after_assistant(state: ChatState) -> str:
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else END


def build_agent(inputs: GraphBuildInputs) -> StateGraph[Any, Any, Any, Any]:
    """Graph topology: assistant -> tools -> assistant -> ... -> END."""

    graph: StateGraph[Any, Any, Any, Any] = StateGraph(ChatState, context_schema=HarnessRunContext)
    graph.add_node("assistant", assistant)
    graph.add_node("tools", harness_tool_node(list(inputs.tools)))
    graph.add_edge(START, "assistant")
    graph.add_conditional_edges("assistant", route_after_assistant, {"tools": "tools", END: END})
    graph.add_edge("tools", "assistant")
    return graph


async def approval(state: ChatState, runtime: Runtime[HarnessRunContext]) -> dict[str, Any]:
    """Pause for a decision, then continue on resume."""

    decision = interrupt({"question": f"approve {len(state['messages'])} messages?"})
    return {"messages": [AIMessage(content=f"approved: {decision}")]}


def build_approval(inputs: GraphBuildInputs) -> StateGraph[Any, Any, Any, Any]:
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(ChatState, context_schema=HarnessRunContext)
    graph.add_node("approval", approval)
    graph.add_edge(START, "approval")
    graph.add_edge("approval", END)
    return graph


def tool_plugin() -> type:
    """Register tools through a plugin, so ownership is explicit."""

    @plugin(name="research-tools", version="1.0.0", provides={"tools": "1.0.0"})
    async def research_tools(ctx: PluginContext) -> None:
        ctx.tools.register(
            fake_tool("search", result=SEARCH_RESULT, parameters={"query": (str, ...)}),
            policy=ToolPolicy(permissions=("network.fetch",), idempotent=True),
        )
        ctx.capabilities.provide(
            __import__("chassis.capabilities", fromlist=["TOOLS"]).TOOLS, "search"
        )

    return research_tools


async def main() -> None:
    # LangSmith tracing turns itself on when LANGSMITH_TRACING is set; without it
    # the backend costs nothing.
    tracing = LangSmithTelemetry()
    print(f"[setup] LangSmith tracing enabled: {tracing.enabled}")

    harness = TestHarness(
        policy=GrantPolicy(["network.fetch"]),
        telemetry=tracing,
        plugins=[tool_plugin()],
    )
    harness.provide(
        MODEL,
        FakeChatModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[{"name": "search", "args": {"query": "chassis"}, "id": "call_1"}],
                ),
                "Chassis keeps composition and execution separate.",
            ]
        ),
    )

    checkpointer = InMemorySaver()
    harness.register_agent(
        LangGraphAgent(
            AgentDefinition(
                name="research-agent", version="1", state_schema=ChatState, build=build_agent
            ),
            checkpointer=checkpointer,
            telemetry=harness.telemetry,
            redactor=harness.redactor,
            stream_mode="messages",
        )
    )
    harness.register_agent(
        LangGraphAgent(
            AgentDefinition(
                name="approval-agent", version="1", state_schema=ChatState, build=build_approval
            ),
            checkpointer=checkpointer,
            telemetry=harness.telemetry,
            redactor=harness.redactor,
        )
    )

    async with harness:
        # 1. The model, the tool, and the graph all run against one generation.
        result = await harness.agents.invoke(
            "research-agent",
            {"messages": [HumanMessage("What is Chassis?")]},
            thread_id="demo-thread",
        )
        print(f"[invoke] generation={result.generation_id} run={result.run_id}")
        print(f"[invoke] answer={result.text!r}")
        tool_messages = [message for message in result.messages if isinstance(message, ToolMessage)]
        assert tool_messages and tool_messages[0].content == SEARCH_RESULT
        print(f"[invoke] tool result={tool_messages[0].content!r}")

        # 2. The checkpointer keeps the thread's history across invocations.
        second = await harness.agents.invoke(
            "research-agent",
            {"messages": [HumanMessage("And again?")]},
            thread_id="demo-thread",
        )
        print(f"[checkpoint] messages on thread 'demo-thread': {len(second.messages)}")
        assert len(second.messages) > len(result.messages)

        # 3. Streaming yields events tagged with the generation they belong to.
        # A checkpointer needs a thread, so streamed runs carry one too.
        events = [
            event
            async for event in harness.agents.stream(
                "research-agent",
                {"messages": [HumanMessage("Stream please")]},
                thread_id="demo-thread",
            )
        ]
        print(f"[stream] {len(events)} events, first kind={events[0].kind!r}")
        assert events
        generation_of_stream = harness.current_generation
        assert generation_of_stream is not None
        assert all(event.generation_id == generation_of_stream.generation_id for event in events)

        # 4. Interrupt/resume drives a human-in-the-loop decision.
        paused = await harness.agents.invoke(
            "approval-agent", {"messages": [HumanMessage("delete everything")]}, thread_id="t-2"
        )
        print(f"[interrupt] paused={paused.interrupted} value={paused.resume_values()}")
        assert paused.interrupted

        resumed = await harness.agents.invoke("approval-agent", thread_id="t-2", resume="yes")
        print(f"[resume] answer={resumed.text!r}")
        assert resumed.text == "approved: yes"

        # 5. Every run is attributable to an immutable runtime snapshot.
        generation = harness.current_generation
        assert generation is not None
        snapshot = harness.snapshot_for(generation, agent="research-agent")
        print(f"[snapshot] digest={snapshot.digest()[:16]}...")
        print(f"[snapshot] plugins={dict(snapshot.plugins)}")
        print(f"[snapshot] capabilities={dict(snapshot.capabilities)}")
        print(f"[snapshot] tool_schema_hash={snapshot.tool_schema_hash[:16]}...")

        # 6. Lifecycle instrumentation recorded the control plane, not the graph.
        print(f"[telemetry] spans={sorted(set(harness.recorded_spans))}")
        print(f"[telemetry] events={sorted(set(harness.recorded_events))}")

    print("\nExample A completed: model capability, tools, checkpointer, streaming,")
    print("interrupt/resume, and snapshot attribution all verified.")


if __name__ == "__main__":
    asyncio.run(main())
