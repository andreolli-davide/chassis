# Chassis

**Chassis** is a production-grade Python agent harness.

It owns the runtime environment in which agents execute — plugins, capabilities,
scoped resources, reversible effects, immutable runtime generations, policy,
budgets, secrets, configuration, diagnostics, and observability metadata — and
hands an immutable view of that environment to an execution engine.

LangGraph is the first-class execution engine *mounted within* Chassis.
Chassis is not a graph framework.

## The core proposition

> Chassis allows agent runtime composition to change over time while active runs
> retain a coherent environment, dependencies react correctly, resources have
> explicit ownership, and obsolete components are disposed only when they are no
> longer reachable.

## A plugin in ten lines

```python
from chassis import DATABASE, PluginContext, plugin
from chassis.tools import ToolPolicy

@plugin(name="web-search", version="1.0.0", provides={"tools": "1.0.0"}, requires={"database": ">=1,<2"})
async def web_search(ctx: PluginContext) -> None:
    store = ctx.require(DATABASE)                       # resolved for this composition
    ctx.tools.register(search_tool, policy=ToolPolicy(permissions=("network.fetch",)))
    ctx.create_task(warm_cache(store), name="cache-warmer")
```

No activation logic, no deregistration calls, no task bookkeeping: the harness
orders plugins by declared capabilities, owns every effect through the plugin's
scope, and cancels its tasks on unload.

## Running an agent

```python
from chassis import MODEL, Harness
from chassis.langgraph import AgentDefinition, LangGraphAgent
from chassis.testing import FakeChatModel

harness = Harness()
harness.provide(MODEL, FakeChatModel(responses=["hello"]))
harness.register_agent(LangGraphAgent(definition, checkpointer=InMemorySaver()))

async with harness:
    result = await harness.agents.invoke("research-agent", {"messages": [...]}, thread_id="t-1")
    print(result.text, result.generation_id)
```

A run acquires one immutable generation and keeps it: swapping a provider publishes
a new generation without mutating the environment underneath an in-flight run.

## What Chassis is *not*

- not a graph engine — LangGraph owns graph execution and durability;
- not a replacement for `langchain-core` models, tools, or runnables;
- not a replacement for LangSmith tracing or experiment management;
- not a sandbox — **in-process Python plugins are trusted code**, and policy is not
  presented as isolation;
- not a system that claims deterministic replay of arbitrary clocks, networks,
  databases, or external services.

## Status

Pre-1.0. The architecture is implemented bottom-up in the order the specification
requires: scope and effects, capabilities, plugin lifecycle, dependency resolution,
runtime generations, boundaries (tools, hooks, policy, secrets, budgets), LangGraph,
observability, configuration, and bounded replay.

## Development

Chassis uses [uv](https://docs.astral.sh/uv/) as its canonical project manager.
Python ≥ 3.12 is required.

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

`pyproject.toml` and `uv.lock` are the canonical dependency state. Do not introduce
alternative project managers or parallel `requirements.txt` files.

### Examples

```bash
uv run python examples/basic_agent.py                 # LangGraph agent end to end
uv run python examples/reactive_cascade.py            # database → memory → extension
uv run python examples/safe_provider_replacement.py   # generations across a provider swap
```

Each example asserts what it prints, so running it verifies the behaviour.

## Documentation

- [`SPEC.md`](SPEC.md) — product and engineering specification.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — architecture baseline and design rationale.
- [`docs/`](docs/README.md) — lifecycle, plugin authoring, LangGraph, observability,
  security assumptions, replay limitations, and configuration.

## Layout

```text
src/chassis/
  core/          scopes, effects, generations, errors
  capabilities/  versioned contracts, provider registry, snapshots
  plugins/       manifests, author API, resolver, registry
  hooks/         scope-owned hook registry
  tasks/         scope-owned background work
  tools/         tool registry and the execution boundary
  policy/        permissions and the evaluation boundary
  budget/        hierarchical budget governor
  secrets/       providers and redaction
  langgraph/     agent definitions, graph cache, LangGraph runtime
  telemetry/     instrumentation protocol, LangSmith, recording
  persistence/   canonical hashing and runtime snapshots
  replay/        bounded record/replay
  config/        declarative configuration and reconciliation
  testing/       TestHarness and fakes
```

## License

Apache-2.0.
