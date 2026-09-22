# Chassis documentation

Rendered at **https://andreolli-davide.github.io/chassis/**.

Current release: **0.9.0**. Install it from
[PyPI](https://pypi.org/project/chassis-harness/0.9.0/) or inspect the
[GitHub Release](https://github.com/andreolli-davide/chassis/releases/tag/v0.9.0).

New here? Start with [getting-started.md](getting-started.md), then use the guides
below for task-oriented detail.

| Document | Read it for |
| --- | --- |
| [getting-started.md](getting-started.md) | install, the smallest useful app, where to go next |
| [why-generations.md](why-generations.md) | the problem immutable generations solve, and what it costs |
| [lifecycle.md](lifecycle.md) | ownership, scopes, plugin lifecycle, generations, safe unload, shutdown |
| [scopes.md](scopes.md) | composition scopes: hierarchy, inheritance, narrow views, provenance, explain and diff |
| [agent-composition.md](agent-composition.md) | AgentSpec, agent revisions, materialization, pinning, retirement, agent diagnostics |
| [incremental-composition.md](incremental-composition.md) | semantic identity, impact analysis, structural sharing, reachability, reuse diagnostics |
| [plugin-author-guide.md](plugin-author-guide.md) | writing, testing, and shipping a plugin |
| [langgraph.md](langgraph.md) | agent definitions, the run context, graph caching, tools, streaming, interrupts |
| [recipes.md](recipes.md) | behind a web service, per-tenant composition, hot provider swap, budgets, durability |
| [troubleshooting.md](troubleshooting.md) | symptom → cause → fix, with the exact diagnostics output |
| [performance.md](performance.md) | benchmark methodology, the 0.9.0 baseline, capacity, and known scaling limits |
| [roadmap.md](roadmap.md) | ordered 0.5.1–0.9.0 audit remediation, acceptance criteria, release gates, and finding traceability |
| [compatibility.md](compatibility.md) | surface classes, the API baseline and compatibility check, deprecations, persisted format versions |
| [production-readiness.md](production-readiness.md) | the 0.9.0 compatibility and operational review: domains, validation matrix, audit resolutions, limitations |
| [observability.md](observability.md) | tracing, snapshots, canonical hashing, redaction, evaluation |
| [security.md](security.md) | trust model, policy, secrets, and what Chassis is *not* |
| [replay.md](replay.md) | record/replay boundaries and their explicit limitations |
| [configuration.md](configuration.md) | declarative configuration, the catalog, reconciliation, drift |
| [planning.md](planning.md) | the machine-readable planning contract and the zero-mutation preview |
| [design.md](design.md) | guarantees, decisions, and deliberate absences |
| [migration.md](migration.md) | every compatibility change from 0.1 through the current release and how to migrate |

## Examples

| Example | Shows |
| --- | --- |
| [examples/production_reference/](https://github.com/andreolli-davide/chassis/tree/main/examples/production_reference) | **the production reference application**: one support-desk system composing configuration, catalog, scopes, AgentSpec, policy, secrets, invoke and streaming, replay, hot replacement with pinned runs, planning, diagnostics, rollback, and graceful shutdown |
| [examples/quickstart.py](https://github.com/andreolli-davide/chassis/blob/main/examples/quickstart.py) | the smallest useful app: a model capability, one LangGraph agent, generation attribution |
| [examples/basic_agent.py](https://github.com/andreolli-davide/chassis/blob/main/examples/basic_agent.py) | a LangGraph agent: model capability, tools, checkpointer, streaming, tracing, interrupt/resume, snapshot |
| [examples/reactive_cascade.py](https://github.com/andreolli-davide/chassis/blob/main/examples/reactive_cascade.py) | `database → memory → agent extension`, removed and restored |
| [examples/safe_provider_replacement.py](https://github.com/andreolli-davide/chassis/blob/main/examples/safe_provider_replacement.py) | an active run keeping its generation across a provider swap |
| [examples/scoped_composition.py](https://github.com/andreolli-davide/chassis/blob/main/examples/scoped_composition.py) | hierarchical scopes: inheritance, sibling isolation, capability narrowing, provenance, diff, pinned runs |
| [examples/agent_composition.py](https://github.com/andreolli-davide/chassis/blob/main/examples/agent_composition.py) | three versioned agents: materialization, narrowing, revision pinning, incremental reuse, retirement |

All six execute their own assertions, so running them is the verification:

```bash
uv run python examples/quickstart.py
uv run python examples/basic_agent.py
uv run python examples/reactive_cascade.py
uv run python examples/safe_provider_replacement.py
uv run python examples/scoped_composition.py
uv run python examples/agent_composition.py
```

## The core proposition

> Chassis allows agent runtime composition to change over time while active runs
> retain a coherent environment, dependencies react correctly, resources have
> explicit ownership, and obsolete components are disposed only when they are no
> longer reachable.
