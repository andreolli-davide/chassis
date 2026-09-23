# Getting started

Five minutes from install to a running agent, and a map of where to go next.

## Install

```bash
pip install chassis-harness==1.0.0b1                        # beta core lifecycle kernel
pip install "chassis-harness[langgraph]==1.0.0b1"           # LangGraph adapter + langchain-core
pip install "chassis-harness[langsmith]==1.0.0b1"           # LangSmith telemetry + evaluation
```

This guide follows the 1.0.0b1 beta. The import package is `chassis`; the
distribution is `chassis-harness`. Python 3.12 and 3.13 are supported. For the
latest stable release, see the [0.9.1 documentation archive](https://andreolli-davide.github.io/chassis/0.9.1/).

The core depends only on `pydantic`, `packaging`, and `pyyaml`; it imports and runs
without `langgraph`, `langchain-core`, or `langsmith`. This page uses the LangGraph
adapter and a scripted model, so install the `langgraph` extra — no credentials are
needed to follow it.

## The smallest useful application

The complete file is [`examples/quickstart.py`](https://github.com/andreolli-davide/chassis/blob/main/examples/quickstart.py); it
asserts everything it prints.

```python
from chassis import MODEL, Harness
from chassis.langgraph import AgentDefinition, GraphBuildInputs, LangGraphAgent
from chassis.runtime import HarnessRunContext
from chassis.testing import FakeChatModel


class ChatState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


async def assistant(state: ChatState, runtime: Runtime[HarnessRunContext]) -> dict:
    model = runtime.context.require_capability(MODEL)   # this run's model
    return {"messages": [await model.ainvoke(state["messages"])]}


def build_agent(inputs: GraphBuildInputs) -> StateGraph:
    graph = StateGraph(ChatState, context_schema=HarnessRunContext)
    graph.add_node("assistant", assistant)
    graph.add_edge(START, "assistant")
    graph.add_edge("assistant", END)
    return graph


async def main() -> None:
    harness = Harness(name="quickstart")
    harness.provide(MODEL, FakeChatModel(responses=["hello from Chassis"]))
    harness.register_agent(
        LangGraphAgent(
            AgentDefinition(name="research-agent", version="1", state_schema=ChatState, build=build_agent),
            checkpointer=InMemorySaver(),
        )
    )

    async with harness:
        result = await harness.agents.invoke(
            "research-agent", {"messages": [HumanMessage("hi")]}, thread_id="thread-1"
        )
        print(result.text, result.generation_id)
```

Run it:

```bash
uv run python examples/quickstart.py
```

Expected output:

```text
[invoke] text='hello from Chassis'
[invoke] generation=gen_0001 thread=thread-1
[invoke] snapshot=20528fce...
[status] {'desired': 1, 'mounted': 1, 'by_state': {'active': 1}} agents=1
```

## What just happened

```text
provide(MODEL, ...)          a capability is registered under a key
register_agent(...)          an agent runtime is registered with the harness
async with harness:          RAII: start reconciles, stop drains and disposes
agents.invoke(...)           the run acquires one immutable *generation* and keeps it
```

The run is attributed to `gen_0001` and to a snapshot digest: the exact plugins,
capability versions, tool schema, and graph definition that produced the answer. If
you swap the model while a run is in flight, that run keeps the old generation and
its old objects — nothing is mutated underneath it.

## Where to go next

| You want to | Read |
| --- | --- |
| build your first plugin | [plugin-author-guide.md](plugin-author-guide.md) |
| wire this into a service, swap providers, set budgets | [recipes.md](recipes.md) |
| fix something that is not working | [troubleshooting.md](troubleshooting.md) |
| understand ownership, generations, unload, shutdown | [lifecycle.md](lifecycle.md) |
| use LangGraph properly (tools, interrupts, streaming, caching) | [langgraph.md](langgraph.md) |
| trace runs, hash snapshots, evaluate experiments | [observability.md](observability.md) |
| know exactly what the security model does and does not promise | [security.md](security.md) |
| record and replay model/tool boundaries | [replay.md](replay.md) |
| drive composition from YAML and reconcile drift | [configuration.md](configuration.md) |
| build hierarchical composition scopes and explain provider choices | [scopes.md](scopes.md) |
| see the design decisions and invariants | [design.md](design.md) |
| upgrade from an earlier release | [migration.md](migration.md) |

The other examples isolate one idea each:

```bash
uv run python examples/basic_agent.py                 # tools, checkpointing, streaming, interrupts, tracing
uv run python examples/reactive_cascade.py            # database → memory → extension, removed and restored
uv run python examples/safe_provider_replacement.py   # generations across a provider swap
uv run python examples/scoped_composition.py          # hierarchical composition scopes
```

## Using it for real

- **Credentials**: read secrets through the provider (`ctx.secrets`), never from the
  environment directly, so they stay out of traces, snapshots, and errors.
- **Durability**: pass a persistent checkpointer (`langgraph.checkpoint.postgres`)
  instead of `InMemorySaver()`. Chassis does not duplicate graph durability.
- **Tracing**: set `LANGSMITH_TRACING=true` and the usual LangSmith variables; the
  harness adds generation metadata on top of native LangGraph tracing.
- **Composition changes**: `install` / `uninstall` / `provide` mark desired state
  dirty; the next run or explicit `reconcile()` publishes a new generation.
- **Diagnostics**: `harness.diagnostics.status()` and
  `harness.diagnostics.explain("my-plugin")` answer "why is this plugin not active"
  from authoritative state; `explain_requirement`, `explain_scope`, and
  `diff_generations` answer the same for scoped composition and provider choice.
