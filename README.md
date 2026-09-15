# Chassis

**Chassis** is a production-grade Python agent harness.

It owns the runtime environment in which agents execute — plugins, capabilities,
scoped resources, reversible effects, immutable runtime generations, policy,
budgets, secrets, configuration, diagnostics, and observability metadata — and
hands an immutable view of that environment to an execution engine.

LangGraph is the first-class execution engine *mounted within* Chassis.
Chassis is not a graph framework.

```text
Scope / reversible effects
        ↓
Capabilities
        ↓
Plugin lifecycle
        ↓
Dependency resolver
        ↓
Runtime generations
        ↓
Leases / draining / disposal
        ↓
Tools / hooks / tasks
        ↓
Policy / secrets / budgets
        ↓
LangGraph integration
        ↓
LangSmith / snapshots
        ↓
Configuration / reconciliation
        ↓
Replay / evaluation
```

## The core proposition

> Chassis allows agent runtime composition to change over time while active runs
> retain a coherent environment, dependencies react correctly, resources have
> explicit ownership, and obsolete components are disposed only when they are no
> longer reachable.

## What Chassis is *not*

- not a graph engine (LangGraph owns graph execution and durability);
- not a replacement for `langchain-core` models, tools, or runnables;
- not a replacement for LangSmith tracing or experiment management;
- not a sandbox — **in-process Python plugins are trusted code**;
- not a system that claims deterministic replay of arbitrary clocks, networks,
  databases, or external services.

## Status

Pre-1.0. The architecture is implemented bottom-up; see `ARCHITECTURE.md` for the
normative invariants and `SPEC.md` for the product specification.

## Development

Chassis uses [uv](https://docs.astral.sh/uv/) as its canonical project manager.
Python >= 3.12 is required.

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

`pyproject.toml` and `uv.lock` are the canonical dependency state. Do not
introduce alternative project managers or parallel `requirements.txt` files.

## Documentation

- [`SPEC.md`](SPEC.md) — product and engineering specification.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — architecture baseline and design rationale.
- [`docs/`](docs/) — lifecycle, plugin-author, LangGraph, LangSmith, security, and
  replay documentation.

## License

Apache-2.0.
