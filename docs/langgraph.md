# LangGraph integration

LangGraph is Chassis's first-class execution engine. Chassis owns composition and
lifecycle; LangGraph owns graph execution, state, durability, streaming, and
interrupts. Chassis never reimplements them, and the lifecycle kernel never imports
LangGraph.

## Wiring an agent

```python
from chassis import Harness
from chassis.langgraph import AgentDefinition, GraphBuildInputs, LangGraphAgent, harness_tool_node

def build(inputs: GraphBuildInputs) -> StateGraph:
    graph = StateGraph(ChatState, context_schema=HarnessRunContext)
    graph.add_node("model", model_node)
    graph.add_node("tools", harness_tool_node(list(inputs.tools)))
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", route, {"tools": "tools", END: END})
    graph.add_edge("tools", "model")
    return graph

harness.register_agent(
    LangGraphAgent(
        AgentDefinition(name="research", version="1", state_schema=ChatState, build=build),
        checkpointer=InMemorySaver(),
        telemetry=harness.telemetry,
        redactor=harness.redactor,
    )
)

result = await harness.agents.invoke("research", {"messages": [HumanMessage("hi")]}, thread_id="t-1")
```

`AgentRuntime` is the boundary:

```python
class AgentRuntime(Protocol):
    @property
    def name(self) -> str: ...
    async def invoke(self, request: AgentRequest, run_context: HarnessRunContext) -> AgentResult: ...
    def stream(self, request: AgentRequest, run_context: HarnessRunContext) -> AsyncIterator[AgentEvent]: ...
```

It is deliberately small: Chassis does not pretend every backend shares identical
durability semantics.

## The run context

Graphs are compiled with `context_schema=HarnessRunContext`, and nodes read their
runtime-bound services from LangGraph's public runtime object:

```python
async def model_node(state, runtime: Runtime[HarnessRunContext]) -> dict:
    model = runtime.context.require_capability(MODEL)
    return {"messages": [await model.ainvoke(state["messages"])]}
```

The context carries the generation id, the run id, the immutable capability
snapshot, identity (`user_id`, `tenant_id`, `thread_id`), the generation's tool and
hook snapshots, and its boundary services. There is no global mutable capability
state, and a node never resolves against "whatever generation is current".

## Build-time vs runtime-bound

Compiling a graph depends only on build-time inputs:

```text
state schema · node topology · static tool composition · middleware topology
```

Changing a runtime-bound provider -- model, database, policy, secrets, tenant --
publishes a new generation with a new capability snapshot and reuses the compiled
graph.

The cache key is composed exclusively of declared build-time inputs:

```text
agent · definition_version · state_schema_hash · tool_schema_hash
· middleware_hash · build_time_capabilities
```

Nothing about the mutable runtime enters it, so "any capability changed" never
invalidates every graph.

`AgentDefinition.version` is the one build-time input that cannot be derived from
data, so it is the contract on authors: **bump it when the node or edge topology
changes.** Everything else invalidates automatically -- change a tool's schema and
the graph is rebuilt; change a model provider and it is not.

`build_time_capabilities` names capabilities whose *versions* affect construction.
A capability that contributes nodes belongs there; a model provider does not.

Inspect the behaviour:

```python
agent.cache.stats          # hits, misses, builds, evictions, invalidations
agent.cache.keys           # the exact keys currently cached
harness.telemetry.events   # graph.compile, graph.cache (hit/miss), graph.cache.invalidate
```

## Tools

`harness_tool_node()` returns a `ToolNode` whose calls flow through the Chassis
boundary, using LangGraph's own `awrap_tool_call` hook rather than a rebuilt node:

```python
graph.add_node("tools", harness_tool_node(list(inputs.tools)))
```

The wrapper reads the per-run context from LangGraph's runtime, so tool calls
execute against the generation the run acquired. See
[plugin-author-guide.md](plugin-author-guide.md#tool-execution) for the order of
hooks, policy, approval, budget, tracing, and normalization.

Synchronous invocation is refused rather than bypassing the boundary. Chassis is
async-first; use `ainvoke`/`astream`.

## Checkpointing, Store, streaming, interrupts

LangGraph owns all of it; Chassis compiles with them and never mirrors durable state
into a second checkpoint system.

```python
LangGraphAgent(definition, checkpointer=InMemorySaver(), store=InMemoryStore())

await harness.agents.invoke("research", input, thread_id="t-1")           # durable thread
await harness.agents.invoke("research", thread_id="t-1", resume="yes")    # resume an interrupt

async for event in harness.agents.stream("research", input, thread_id="t-1", stream_mode="messages"):
    ...
```

- An interrupt surfaces as `result.interrupted` with `result.resume_values()`.
- Resuming invokes the same thread with `Command(resume=...)` under the hood.
- A checkpointer requires a `thread_id`; LangGraph raises when one is missing.
- `stream_mode` is a constructor argument; multiple modes yield
  `AgentEvent(kind=mode, data=chunk)`.

Chassis persistence is separate and covers different concerns: plugin
configuration, desired state, generation metadata, runtime snapshots, and replay
records.

## Result shape

```python
AgentResult(
    agent, generation_id, run_id, output, thread_id, interrupts,
    duration_seconds, metadata,   # includes snapshot_digest
)
result.messages   # messages from the output, when the graph produced a list
result.text       # text of the most recent message carrying string content
```

## Testing graphs

`TestHarness.agent(...)` wires the harness's telemetry and redactor for you, and
`FakeChatModel` scripts model responses (including tool calls) without a network:

```python
async with TestHarness() as harness:
    harness.provide(MODEL, FakeChatModel(responses=[scripted_tool_call, "final answer"]))
    harness.install_tools(my_tool, policies={"my_tool": ToolPolicy(permissions=("network.fetch",))})
    harness.register_agent(harness.agent(definition))
    result = await harness.agents.invoke("research", {"messages": [HumanMessage("hi")]})
```

[examples/basic_agent.py](../examples/basic_agent.py) runs this end to end,
including streaming, interrupt/resume, and snapshot attribution.

## What Chassis deliberately does not do

- no second graph engine or state machine;
- no parallel tool ecosystem: tools stay `langchain-core` objects;
- no re-implementation of checkpointing, Store, interrupts, or streaming;
- no rebuilding every graph when a runtime-bound capability changes.
