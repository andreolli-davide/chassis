# Chassis

**Chassis** is a production-grade Python agent harness.

It owns the runtime environment in which agents execute — plugins, capabilities,
scoped resources, reversible effects, immutable runtime generations, policy,
budgets, secrets, configuration, diagnostics, and observability metadata — and
hands an immutable view of that environment to an execution engine.

LangGraph is the first-class execution engine *mounted within* Chassis.
Chassis is not a graph framework.

## Install

```bash
pip install chassis-harness
```

Python 3.12+. The import package is `chassis`.

## Quickstart

```python
from chassis import MODEL, Harness
from chassis.langgraph import AgentDefinition, GraphBuildInputs, LangGraphAgent
from chassis.runtime import HarnessRunContext

harness = Harness(name="quickstart")
harness.provide(MODEL, my_chat_model)                      # a langchain-core chat model
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
    print(result.text, result.generation_id)        # hello … gen_0001
```

A run acquires one immutable generation and keeps it: swapping a provider publishes
a new generation without mutating the environment underneath an in-flight run.

Runnable end to end — no credentials, scripted model, asserts its own output:

```bash
uv run python examples/quickstart.py
```

Full walkthrough: [docs/getting-started.md](docs/getting-started.md).

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

## The core proposition

> Chassis allows agent runtime composition to change over time while active runs
> retain a coherent environment, dependencies react correctly, resources have
> explicit ownership, and obsolete components are disposed only when they are no
> longer reachable.

[Guarantees and deliberate absences](docs/design.md) states what that buys you, and
what Chassis refuses to promise.

## What Chassis is *not*

- not a graph engine — LangGraph owns graph execution and durability;
- not a replacement for `langchain-core` models, tools, or runnables;
- not a replacement for LangSmith tracing or experiment management;
- not a sandbox — **in-process Python plugins are trusted code**, and policy is not
  presented as isolation;
- not a system that claims deterministic replay of arbitrary clocks, networks,
  databases, or external services.

## Status

Pre-1.0 (`0.1.0`). The surface covered by `tests/test_public_api.py` may break in a
minor release; every break is recorded in [CHANGELOG.md](CHANGELOG.md).

## Development

Chassis uses [uv](https://docs.astral.sh/uv/) as its canonical project manager.
Python ≥ 3.12 is required.

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv build
```

`pyproject.toml` and `uv.lock` are the canonical dependency state. Do not introduce
alternative project managers or parallel `requirements.txt` files.

### Releasing

1. record the change in `CHANGELOG.md` under a `## [x.y.z]` heading;
2. bump `version` in `pyproject.toml` to the same number;
3. tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`.

`.github/workflows/release.yml` runs the full check suite, then refuses to build
unless the tag, the project version, and the changelog agree; it builds the
distribution, installs the wheel into a clean environment, runs the quickstart
against it, and publishes through PyPI trusted publishing (no token is stored).
`workflow_dispatch` verifies all of that without publishing.

### Examples

```bash
uv run python examples/quickstart.py                  # smallest useful app
uv run python examples/basic_agent.py                 # LangGraph agent end to end
uv run python examples/reactive_cascade.py            # database → memory → extension
uv run python examples/safe_provider_replacement.py   # generations across a provider swap
```

Each example asserts what it prints, so running it verifies the behaviour. The test
suite runs all of them.

## Documentation

- [`docs/getting-started.md`](docs/getting-started.md) — install and first run.
- [`docs/recipes.md`](docs/recipes.md) — behind a web service, per-tenant composition,
  hot provider swaps, budgets, durable runs.
- [`docs/troubleshooting.md`](docs/troubleshooting.md) — symptom, cause, and the exact
  diagnostics output for each.
- [`docs/`](docs/README.md) — lifecycle, plugin authoring, LangGraph, observability,
  security assumptions, replay limitations, configuration, and design guarantees.

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

## Contributing

Issues and pull requests are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md)
for the gates a change must pass and what a reviewable commit looks like. Questions
belong in [Discussions](https://github.com/andreolli-davide/chassis/discussions);
security reports go through
[private advisories](https://github.com/andreolli-davide/chassis/security/advisories/new)
instead of a public issue — see [SECURITY.md](SECURITY.md) for what is in scope.

## License

Apache-2.0 — see [LICENSE](LICENSE).
