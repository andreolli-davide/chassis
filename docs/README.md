# Chassis documentation

Start with the specification and the architecture baseline, then use the guides
below for task-oriented detail.

| Document | Read it for |
| --- | --- |
| [../SPEC.md](../SPEC.md) | the normative product and engineering specification |
| [../ARCHITECTURE.md](../ARCHITECTURE.md) | architectural invariants, decisions, and trade-offs |
| [lifecycle.md](lifecycle.md) | ownership, scopes, plugin lifecycle, generations, safe unload, shutdown |
| [plugin-author-guide.md](plugin-author-guide.md) | writing, testing, and shipping a plugin |
| [langgraph.md](langgraph.md) | agent definitions, the run context, graph caching, tools, streaming, interrupts |
| [observability.md](observability.md) | tracing, snapshots, canonical hashing, redaction, evaluation |
| [security.md](security.md) | trust model, policy, secrets, and what Chassis is *not* |
| [replay.md](replay.md) | record/replay boundaries and their explicit limitations |
| [configuration.md](configuration.md) | declarative configuration, the catalog, reconciliation, drift |

## Examples

| Example | Shows |
| --- | --- |
| [../examples/basic_agent.py](../examples/basic_agent.py) | a LangGraph agent: model capability, tools, checkpointer, streaming, tracing, interrupt/resume, snapshot |
| [../examples/reactive_cascade.py](../examples/reactive_cascade.py) | `database → memory → agent extension`, removed and restored |
| [../examples/safe_provider_replacement.py](../examples/safe_provider_replacement.py) | an active run keeping its generation across a provider swap |

All three execute their own assertions, so running them is the verification:

```bash
uv run python examples/basic_agent.py
uv run python examples/reactive_cascade.py
uv run python examples/safe_provider_replacement.py
```

## The core proposition

> Chassis allows agent runtime composition to change over time while active runs
> retain a coherent environment, dependencies react correctly, resources have
> explicit ownership, and obsolete components are disposed only when they are no
> longer reachable.
