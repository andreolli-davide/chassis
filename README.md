# Chassis

[![CI](https://github.com/andreolli-davide/chassis/actions/workflows/ci.yml/badge.svg)](https://github.com/andreolli-davide/chassis/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/chassis-harness)](https://pypi.org/project/chassis-harness/)
[![Python](https://img.shields.io/pypi/pyversions/chassis-harness)](https://pypi.org/project/chassis-harness/)
[![License](https://img.shields.io/pypi/l/chassis-harness)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-mkdocs--material-blue)](https://andreolli-davide.github.io/chassis/)

**Chassis** is a transactional runtime composition layer for dynamic agent systems.

It owns the runtime environment in which agents execute — plugins, capabilities,
scoped resources, reversible effects, immutable runtime generations, policy,
budgets, secrets, configuration, diagnostics, and observability metadata — and
hands an immutable view of that environment to an execution engine.

Composition may change over time; every in-flight run stays pinned to the immutable
runtime generation it started with. LangGraph is the first-class execution engine
*mounted within* Chassis. Chassis is not a graph framework.

## Install

```bash
pip install chassis-harness                       # the core lifecycle kernel
pip install "chassis-harness[langgraph]"          # the LangGraph adapter
pip install "chassis-harness[langsmith]"          # LangSmith telemetry + evaluation
pip install "chassis-harness[langgraph,langsmith]"
```

Python 3.12+. The import package is `chassis`.

The core has no dependency on `langgraph`, `langchain-core`, or `langsmith`:
importing `chassis`, the plugin lifecycle, generations, budgets, and diagnostics all
work without them. The extras add the LangGraph adapter, the langchain-core test
doubles, and the LangSmith backend; using an integration whose extra is missing
raises a `MissingExtraError` that names the extra to install.

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

The LangGraph quickstart needs the adapter extra:
`pip install "chassis-harness[langgraph]"`.

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

## Hierarchical composition scopes

Composition can be a tree, not a list. A scope inherits the providers visible from
its ancestors, adds its own, narrows what it exposes, and owns what it declares —
and it is still only desired state until a generation is published:

```python
harness.install(postgres, entry_id="postgres")                 # shared, at the root
tier = harness.composition.child("tier-a", capabilities=[MODEL, DATABASE])
research = tier.child("research")
research.install(search, entry_id="search")
research.require(MODEL, ">=1,<2")

async with harness:
    harness.diagnostics.explain_requirement("agent", "database").to_text()
    harness.diagnostics.explain_scope("/tier-a/research").to_dict()
    harness.diagnostics.diff_generations(old_id, new_id).to_text()
```

Sibling-local composition stays invisible, an ambiguity between a local and an
inherited provider stays explicit until a preference resolves it, and a run that
acquired a scope tree keeps observing exactly that tree.

Full guide: [docs/scopes.md](docs/scopes.md). Runnable:
`uv run python examples/scoped_composition.py`.

## Versioned agent composition

A logical agent is described once, versioned, and materialized into the same
composition scopes — without Chassis becoming an agent framework:

```python
from chassis.agent_spec import AgentSpec

harness.agents.install(AgentSpec(
    name="finance",
    revision="17",
    runtime_ref="finance-graph",                      # a registered AgentRuntime
    capabilities=["model", "database", "tools"],      # visibility, not authority
    requires={"model": ">=1,<2", "database": ">=1,<2"},
    tools=["spreadsheet"],
    profile="reasoning",
    plugins={"ledger": {}},                           # resolved via the catalog
))

result = await harness.agents.invoke("finance", {"messages": [...]})
assert (result.agent, result.agent_revision) == ("finance", "17")

harness.diagnostics.explain_agent("finance", revision="17").to_text()
harness.diagnostics.diff_agents("finance", "17", "18").to_text()
```

A revision, once published, is immutable; a run keeps the revision (and generation)
it started under even when a newer revision is published; retiring an agent stops new
runs without destroying old executions; and unchanged contributions are reused across
a revision change by the same semantic-identity machinery everything else uses.

`AgentSpec` is what composition an agent sees; LangGraph's `AgentDefinition` stays how
a specific engine builds and runs a graph. The core imports without LangGraph.

Full guide: [docs/agent-composition.md](docs/agent-composition.md). Runnable:
`uv run python examples/agent_composition.py`.

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

Pre-1.0 (`0.5.0`). The surface covered by `tests/test_public_api.py` may break in a
minor release; every break is recorded in [CHANGELOG.md](CHANGELOG.md), and
[migrations](docs/migration.md) lists the 0.1 → 0.2, 0.2 → 0.3, 0.3 → 0.4, and
0.4 → 0.5 changes.

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
uv run python examples/scoped_composition.py          # hierarchical composition scopes
uv run python examples/agent_composition.py           # versioned agents: revisions, pinning, reuse
```

Each example asserts what it prints, so running it verifies the behaviour. The test
suite runs all of them.

## Documentation

- Rendered docs: **https://andreolli-davide.github.io/chassis/**
- [`docs/getting-started.md`](docs/getting-started.md) — install and first run.
- [`docs/recipes.md`](docs/recipes.md) — behind a web service, per-tenant composition,
  hot provider swaps, budgets, durable runs.
- [`docs/troubleshooting.md`](docs/troubleshooting.md) — symptom, cause, and the exact
  diagnostics output for each.
- [`docs/migration.md`](docs/migration.md) — the 0.4 → 0.5, 0.3 → 0.4, 0.2 → 0.3, and 0.1 → 0.2 changes and how to migrate.
- [`docs/agent-composition.md`](docs/agent-composition.md) — `AgentSpec`, immutable agent
  revisions, materialization, revision pinning, retirement, and agent diagnostics.
- [`docs/incremental-composition.md`](docs/incremental-composition.md) — how composition
  changes incrementally: semantic identity, impact analysis, structural sharing, reuse diagnostics.
- [`docs/`](docs/README.md) — lifecycle, scoped composition, plugin authoring, LangGraph, observability,
  security assumptions, replay limitations, configuration, and design guarantees.

## Layout

```text
src/chassis/
  core/          scopes, effects, generations, errors
  composition.py composition scopes and resolved scope trees
  agent_spec.py  AgentSpec and AgentRevision (agent composition descriptions)
  agents.py      agent revision registry, materialization, invocation
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
