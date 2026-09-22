from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from typing_extensions import TypedDict

from chassis import MODEL
from chassis.agent_spec import AgentSpec
from chassis.capabilities import CapabilityKey
from chassis.core.errors import GraphBuildError, HarnessStateError
from chassis.langgraph import (
    AgentDefinition,
    GraphBuildInputs,
    GraphCache,
    LangGraphAgent,
    harness_tool_node,
)
from chassis.plugins.lifecycle import PluginState
from chassis.runtime import HarnessRunContext
from chassis.testing import FakeChatModel, FakePolicy, TestHarness, fake_tool
from chassis.tools import ToolPolicy

TOOLS = CapabilityKey("tools", "1")


class ChatState(TypedDict):
    """Graph state: a message list with LangGraph's additive reducer."""

    messages: Annotated[list[AnyMessage], add_messages]


async def model_node(state: Any, runtime: Runtime[HarnessRunContext]) -> dict[str, Any]:
    model = runtime.context.require_capability(MODEL)
    response = await model.ainvoke(state["messages"])
    return {"messages": [response]}


def should_continue(state: Any) -> str:
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else END


def agent_definition(
    *,
    name: str = "echo-agent",
    version: str = "1",
    with_tools: bool = True,
    build_time_capabilities: Sequence[str] = (),
) -> AgentDefinition:
    def build(inputs: GraphBuildInputs) -> StateGraph[Any, Any, Any, Any]:
        graph: StateGraph[Any, Any, Any, Any] = StateGraph(
            ChatState, context_schema=HarnessRunContext
        )
        graph.add_node("model", model_node)
        graph.add_edge(START, "model")
        if with_tools and inputs.tools:
            graph.add_node("tools", harness_tool_node(list(inputs.tools)))
            graph.add_conditional_edges("model", should_continue, {"tools": "tools", END: END})
            graph.add_edge("tools", "model")
        else:
            graph.add_edge("model", END)
        return graph

    return AgentDefinition(
        name=name,
        version=version,
        state_schema=ChatState,
        build=build,
        build_time_capabilities=tuple(build_time_capabilities),
    )


async def test_agent_invocation_publishes_a_generation_and_returns_output() -> None:

    async with TestHarness() as harness:
        harness.provide(MODEL, FakeChatModel(responses=["hello from the model"]))
        agent = harness.agent(agent_definition(with_tools=False))
        harness.register_agent(agent)

        result = await harness.agents.invoke("echo-agent", {"messages": [HumanMessage("hi")]})

        assert result.agent == "echo-agent"
        assert result.generation_id == harness.current_generation.generation_id  # type: ignore[union-attr]
        assert result.text == "hello from the model"
        assert result.duration_seconds > 0
        assert isinstance(result.output["messages"][-1], AIMessage)


async def test_nodes_read_capabilities_from_the_run_context() -> None:

    seen: list[str] = []

    async def recording_node(state: Any, runtime: Runtime[HarnessRunContext]) -> dict[str, Any]:
        model = runtime.context.require_capability(MODEL)
        seen.append(runtime.context.generation_id)
        answer = await model.ainvoke(state["messages"])
        return {"messages": [answer]}

    def build(inputs: GraphBuildInputs) -> StateGraph[Any, Any, Any, Any]:
        graph: StateGraph[Any, Any, Any, Any] = StateGraph(
            ChatState, context_schema=HarnessRunContext
        )
        graph.add_node("model", recording_node)
        graph.add_edge(START, "model")
        graph.add_edge("model", END)
        return graph

    async with TestHarness() as harness:
        harness.provide(MODEL, FakeChatModel(responses=["ok"]))
        agent = harness.agent(
            AgentDefinition(name="recording", version="1", state_schema=ChatState, build=build)
        )
        harness.register_agent(agent)

        result = await harness.agents.invoke("recording", {"messages": [HumanMessage("hi")]})

        assert seen == [result.generation_id]


async def test_compiled_graph_is_reused_across_runs_and_generations() -> None:

    async with TestHarness() as harness:
        harness.provide(MODEL, FakeChatModel(responses=["first"]))
        agent = harness.agent(agent_definition(with_tools=False))
        harness.register_agent(agent)

        await harness.agents.invoke("echo-agent", {"messages": [HumanMessage("one")]})
        await harness.agents.invoke("echo-agent", {"messages": [HumanMessage("two")]})

        assert agent.cache.stats.builds == 1
        assert agent.cache.stats.hits == 1
        assert "graph.compile" in harness.telemetry.event_names()


async def test_runtime_bound_provider_swap_does_not_recompile_the_graph() -> None:

    async with TestHarness() as harness:
        harness.provide(MODEL, FakeChatModel(responses=["from provider A"]))
        await harness.start()
        agent = harness.agent(agent_definition(with_tools=False))
        harness.register_agent(agent)

        first = await harness.agents.invoke("echo-agent", {"messages": [HumanMessage("hi")]})
        assert first.text == "from provider A"

        # A runtime-bound capability changes: a new generation, same compiled graph.
        harness.provide(MODEL, FakeChatModel(responses=["from provider B"]))
        await harness.reconcile()

        second = await harness.agents.invoke("echo-agent", {"messages": [HumanMessage("hi")]})

        assert second.text == "from provider B"
        assert second.generation_id != first.generation_id
        assert agent.cache.stats.builds == 1
        assert agent.cache.stats.hits == 1


async def test_build_time_capability_change_invalidates_the_graph() -> None:

    async with TestHarness() as harness:
        harness.provide(MODEL, FakeChatModel(responses=["ok"]))
        await harness.start()
        agent = harness.agent(
            agent_definition(with_tools=False, build_time_capabilities=("tools",))
        )
        harness.register_agent(agent)

        await harness.agents.invoke("echo-agent", {"messages": [HumanMessage("hi")]})
        assert agent.cache.stats.builds == 1

        # A declared build-time capability appears: the graph must be rebuilt.
        harness.install_tools(fake_tool("echo", result="echoed"))
        await harness.reconcile()
        await harness.agents.invoke("echo-agent", {"messages": [HumanMessage("hi")]})

        assert agent.cache.stats.builds == 2
        assert len(agent.cache) == 2


async def test_tool_schema_change_invalidates_the_graph() -> None:

    async with TestHarness() as harness:
        harness.provide(MODEL, FakeChatModel(responses=["ok"]))
        harness.install_tools(fake_tool("echo", result="one"))
        await harness.start()
        agent = harness.agent(agent_definition())
        harness.register_agent(agent)

        await harness.agents.invoke("echo-agent", {"messages": [HumanMessage("hi")]})
        assert agent.cache.stats.builds == 1

        harness.install_tools(fake_tool("other", result="two"))
        await harness.reconcile()
        await harness.agents.invoke("echo-agent", {"messages": [HumanMessage("hi")]})

        assert agent.cache.stats.builds == 2
        assert len(agent.cache) == 2


async def test_definition_version_change_invalidates_the_graph() -> None:

    async with TestHarness() as harness:
        harness.provide(MODEL, FakeChatModel(responses=["ok"]))
        await harness.start()
        agent_v1 = harness.agent(agent_definition(version="1", with_tools=False))
        harness.register_agent(agent_v1)
        await harness.agents.invoke("echo-agent", {"messages": [HumanMessage("hi")]})

        agent_v2 = harness.agent(agent_definition(version="2", with_tools=False))
        harness.register_agent(agent_v2, replace=True)
        await harness.agents.invoke("echo-agent", {"messages": [HumanMessage("hi")]})

        assert agent_v1.cache.stats.builds == 1
        assert agent_v2.cache.stats.builds == 1
        assert agent_v1.cache.stats.hits == 0


async def test_checkpointer_persists_thread_state_across_invocations() -> None:

    checkpointer = InMemorySaver()
    async with TestHarness() as harness:
        harness.provide(MODEL, FakeChatModel(responses=["remembered"]))
        agent = LangGraphAgent(agent_definition(with_tools=False), checkpointer=checkpointer)
        harness.register_agent(agent)

        first = await harness.agents.invoke(
            "echo-agent", {"messages": [HumanMessage("first")]}, thread_id="thread-1"
        )
        second = await harness.agents.invoke(
            "echo-agent", {"messages": [HumanMessage("second")]}, thread_id="thread-1"
        )

        assert first.thread_id == "thread-1"
        assert len(second.messages) == 4  # two human turns and two model replies
        assert [
            message.content for message in second.messages if isinstance(message, HumanMessage)
        ] == [
            "first",
            "second",
        ]


async def test_interrupt_and_resume_round_trip() -> None:

    async def approval_node(state: Any, runtime: Runtime[HarnessRunContext]) -> dict[str, Any]:
        answer = interrupt({"question": "approve?"})
        return {"messages": [AIMessage(content=f"answer={answer}")]}

    def build(inputs: GraphBuildInputs) -> StateGraph[Any, Any, Any, Any]:
        graph: StateGraph[Any, Any, Any, Any] = StateGraph(
            ChatState, context_schema=HarnessRunContext
        )
        graph.add_node("approval", approval_node)
        graph.add_edge(START, "approval")
        graph.add_edge("approval", END)
        return graph

    checkpointer = InMemorySaver()
    async with TestHarness() as harness:
        harness.provide(MODEL, FakeChatModel(responses=["unused"]))
        harness.register_agent(
            harness.agent(
                AgentDefinition(name="approval", version="1", state_schema=ChatState, build=build),
                checkpointer=checkpointer,
            )
        )

        interrupted = await harness.agents.invoke(
            "approval", {"messages": [HumanMessage("start")]}, thread_id="thread-9"
        )

        assert interrupted.interrupted
        assert interrupted.resume_values() == ({"question": "approve?"},)

        resumed = await harness.agents.invoke("approval", thread_id="thread-9", resume="yes")

        assert not resumed.interrupted
        assert resumed.text == "answer=yes"


async def test_streaming_yields_events() -> None:

    async with TestHarness() as harness:
        harness.provide(MODEL, FakeChatModel(responses=["streamed answer"]))
        agent = harness.agent(agent_definition(with_tools=False), stream_mode="updates")
        harness.register_agent(agent)

        events = [
            event
            async for event in harness.agents.stream(
                "echo-agent", {"messages": [HumanMessage("hi")]}
            )
        ]

        assert [event.kind for event in events] == ["updates"]
        assert all(event.generation_id for event in events)
        assert any("model" in payload for event in events for payload in [event.data])


async def test_token_streaming_emits_message_chunks() -> None:

    async with TestHarness() as harness:
        harness.provide(MODEL, FakeChatModel(responses=["abcdefghijkl"]))
        agent = harness.agent(agent_definition(with_tools=False), stream_mode="messages")
        harness.register_agent(agent)

        chunks = [
            event
            async for event in harness.agents.stream(
                "echo-agent", {"messages": [HumanMessage("hi")]}
            )
        ]

        assert chunks
        assert all(event.kind == "messages" for event in chunks)

        # The chunks must reassemble into exactly what the model produced.
        text = "".join(str(event.data[0].content) for event in chunks)
        assert text == "abcdefghijkl"


async def test_harness_mediated_tool_execution_succeeds_through_tool_node() -> None:

    calls: list[tuple[str, dict[str, Any]]] = []
    tool = fake_tool("echo", result="echoed!", parameters={"text": (str, ...)}, calls=calls)
    scripted = AIMessage(
        content="",
        tool_calls=[{"name": "echo", "args": {"text": "hi"}, "id": "call_1"}],
    )

    async with TestHarness(policy=FakePolicy(["network.fetch"])) as harness:
        harness.provide(MODEL, FakeChatModel(responses=[scripted, "final answer"]))
        harness.install_tools(tool, policies={"echo": ToolPolicy(permissions=("network.fetch",))})
        harness.register_agent(LangGraphAgent(agent_definition()))

        result = await harness.agents.invoke("echo-agent", {"messages": [HumanMessage("hi")]})

        assert calls == [("echo", {"text": "hi"})]
        assert result.text == "final answer"
        tool_messages = [m for m in result.messages if isinstance(m, ToolMessage)]
        assert tool_messages[0].content == "echoed!"
        assert tool_messages[0].status == "success"


async def test_policy_denial_becomes_an_error_tool_message() -> None:

    calls: list[tuple[str, dict[str, Any]]] = []
    tool = fake_tool("rm", result="deleted", parameters={"path": (str, ...)}, calls=calls)
    scripted = AIMessage(
        content="", tool_calls=[{"name": "rm", "args": {"path": "/etc/passwd"}, "id": "call_1"}]
    )

    async with TestHarness(
        policy=__import__("chassis.testing", fromlist=["FakePolicy"]).FakePolicy(["network.fetch"])
    ) as harness:
        harness.provide(MODEL, FakeChatModel(responses=[scripted, "understood"]))
        harness.install_tools(tool, policies={"rm": ToolPolicy(permissions=("filesystem.delete",))})
        harness.register_agent(LangGraphAgent(agent_definition()))

        result = await harness.agents.invoke("echo-agent", {"messages": [HumanMessage("hi")]})

        assert calls == []  # the tool never ran
        tool_messages = [m for m in result.messages if isinstance(m, ToolMessage)]
        assert tool_messages[0].status == "error"
        assert "filesystem.delete" in str(tool_messages[0].content)
        assert result.text == "understood"


async def test_synchronous_execution_is_refused_rather_than_bypassing_the_boundary() -> None:
    """A sync graph call must fail loudly instead of skipping the tool boundary."""

    def tools_only_build(inputs: GraphBuildInputs) -> StateGraph[Any, Any, Any, Any]:
        graph: StateGraph[Any, Any, Any, Any] = StateGraph(
            ChatState, context_schema=HarnessRunContext
        )
        graph.add_node("tools", harness_tool_node(list(inputs.tools)))
        graph.add_edge(START, "tools")
        graph.add_edge("tools", END)
        return graph

    calls: list[tuple[str, dict[str, Any]]] = []
    async with TestHarness() as harness:
        harness.install_tools(
            fake_tool("echo", result="x", parameters={"text": (str, ...)}, calls=calls)
        )
        await harness.start()
        agent = harness.agent(
            AgentDefinition(
                name="tools-only", version="1", state_schema=ChatState, build=tools_only_build
            )
        )
        harness.register_agent(agent)

        generation = harness.current_generation
        assert generation is not None
        run_context = HarnessRunContext.new(
            generation=generation, environment=harness.run_environment(generation)
        )
        graph = agent.graph(run_context)
        tool_call = AIMessage(
            content="", tool_calls=[{"name": "echo", "args": {"text": "hi"}, "id": "c1"}]
        )

        with pytest.raises(HarnessStateError) as excinfo:
            graph.invoke({"messages": [tool_call]})

        assert "async execution" in excinfo.value.message
        assert calls == []


async def test_graph_build_failure_is_reported_as_a_build_error() -> None:

    def failing_build(inputs: GraphBuildInputs) -> StateGraph[Any, Any, Any, Any]:
        raise RuntimeError("cannot build")

    async with TestHarness() as harness:
        harness.provide(MODEL, FakeChatModel(responses=["ok"]))
        harness.register_agent(
            harness.agent(
                AgentDefinition(
                    name="broken", version="1", state_schema=ChatState, build=failing_build
                )
            )
        )

        with pytest.raises(GraphBuildError) as excinfo:
            await harness.agents.invoke("broken", {"messages": [HumanMessage("hi")]})

        assert excinfo.value.context["agent"] == "broken"


async def test_agent_registration_is_scope_owned() -> None:
    definition = agent_definition(with_tools=False)
    async with TestHarness() as harness:
        scope = harness.scope
        harness.register_agent(harness.agent(definition, cache=GraphCache()), scope=scope)

        assert "echo-agent" in harness.agents
        assert scope.effects[0].kind == "agent"


async def test_agent_unloads_with_its_plugin() -> None:
    from chassis import PluginContext, plugin

    @plugin(name="agent-provider", version="1.0.0")
    async def provider(ctx: PluginContext) -> None:
        ctx.agents.register(LangGraphAgent(agent_definition(with_tools=False), cache=GraphCache()))

    async with TestHarness(plugins=[provider]) as harness:
        assert "echo-agent" in harness.agents

    assert len(harness.agents) == 0


async def test_cache_is_bounded_and_evicts_oldest() -> None:
    cache = GraphCache(max_entries=1)

    async with TestHarness() as harness:
        harness.provide(MODEL, FakeChatModel(responses=["ok"]))
        await harness.start()
        agent = harness.agent(agent_definition(with_tools=False), cache=cache)
        harness.register_agent(agent)
        await harness.agents.invoke("echo-agent", {"messages": [HumanMessage("a")]})

        harness.install_tools(fake_tool("echo", result="x"))
        await harness.reconcile()
        await harness.agents.invoke("echo-agent", {"messages": [HumanMessage("b")]})

        assert len(cache) == 1
        assert cache.stats.evictions == 1


async def test_active_run_keeps_its_generation_while_provider_is_replaced() -> None:

    async with TestHarness() as harness:
        harness.provide(MODEL, FakeChatModel(responses=["provider A"]))
        await harness.start()
        agent = harness.agent(agent_definition(with_tools=False))
        harness.register_agent(agent)

        generation_one = harness.current_generation
        assert generation_one is not None

        harness.provide(MODEL, FakeChatModel(responses=["provider B"]))
        await harness.reconcile()

        # A run started now observes the new generation; the old snapshot still
        # resolves the old provider for anyone holding it.
        assert generation_one.snapshot.require(MODEL).model_name == "fake-model"
        result = await harness.agents.invoke("echo-agent", {"messages": [HumanMessage("hi")]})
        assert result.text == "provider B"
        assert result.generation_id != generation_one.generation_id
        assert harness.plugin_registry.instance("chassis.services") is not None
        assert (
            harness.plugin_registry.instance("chassis.services").state is PluginState.ACTIVE  # type: ignore[union-attr]
        )


async def test_agent_spec_binds_to_a_langgraph_agent_definition() -> None:
    """AgentSpec is composition; AgentDefinition is the graph-build contract."""

    async with TestHarness() as harness:
        harness.provide(MODEL, FakeChatModel(responses=["hello from finance"]))
        # The graph's own name differs from the logical agent: the spec's
        # runtime_ref is what binds them, and attribution follows the agent.
        harness.register_agent(
            harness.agent(agent_definition(name="finance-graph", with_tools=False))
        )
        harness.agents.install(
            AgentSpec(name="finance", revision="17", runtime_ref="finance-graph")
        )

        result = await harness.agents.invoke("finance", {"messages": [HumanMessage("hi")]})

        assert result.agent == "finance"
        assert result.agent_revision == "17"
        assert result.text == "hello from finance"
        assert result.metadata["runtime"] == "langgraph"
        assert result.metadata["snapshot_digest"]


# --------------------------------------------------------------------------
# Runtime identity and graph cache validation (R020).
# --------------------------------------------------------------------------


async def test_snapshots_report_the_selected_runtime_identity() -> None:
    from chassis.evaluation import composition_metadata
    from chassis.testing import TestHarness

    class CustomRuntime:
        @property
        def name(self) -> str:
            return "custom"

        async def invoke(self, request: Any, run_context: Any) -> Any:
            raise NotImplementedError

        async def stream(self, request: Any, run_context: Any) -> Any:
            raise NotImplementedError
            yield  # pragma: no cover - protocol shape only

    async with TestHarness() as harness:
        harness.register_agent(CustomRuntime())  # type: ignore[arg-type]
        await harness.reconcile()

        # The identity comes from the selected runtime; a custom runtime is
        # never labelled `langgraph`, and no runtime means `unknown`.
        assert composition_metadata(harness)["agent_runtime"] == "unknown"
        assert composition_metadata(harness, agent="custom")["agent_runtime"] == "CustomRuntime"


def test_graph_cache_capacity_is_validated_and_zero_disables() -> None:
    from chassis.core.errors import ConfigurationError
    from chassis.langgraph.graphs import AgentDefinition, GraphCache, build_cache_key

    with pytest.raises(ConfigurationError):
        GraphCache(max_entries=-1)

    def build(inputs: Any) -> Any:
        raise NotImplementedError

    definition = AgentDefinition(
        name="cache-probe", version="1", state_schema=ChatState, build=build
    )
    key = build_cache_key(definition, tools=[], build_time_versions=None)

    # Zero disables caching: nothing is retained.
    cache = GraphCache(max_entries=0)
    cache.put(key, "graph")  # type: ignore[arg-type]
    assert cache.get(key) is None


def test_captured_static_inputs_require_a_version_change() -> None:
    from chassis.langgraph.graphs import AgentDefinition, build_cache_key

    def build_first(inputs: Any) -> Any:
        raise NotImplementedError

    def build_second(inputs: Any) -> Any:
        raise NotImplementedError

    first = AgentDefinition(name="dup", version="1", state_schema=ChatState, build=build_first)
    second = AgentDefinition(name="dup", version="1", state_schema=ChatState, build=build_second)

    # The key cannot see captured static inputs: two different builders collide
    # on one key. That hazard is why topology or captured static changes require
    # a version bump.
    assert build_cache_key(first, tools=[], build_time_versions=None) == build_cache_key(
        second, tools=[], build_time_versions=None
    )

    bumped = AgentDefinition(name="dup", version="2", state_schema=ChatState, build=build_first)
    assert build_cache_key(first, tools=[], build_time_versions=None) != build_cache_key(
        bumped, tools=[], build_time_versions=None
    )
