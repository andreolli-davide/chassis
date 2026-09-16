# Lifecycle and ownership

Chassis owns runtime composition and lifecycle. This document describes the
guarantees that make it safe to change an agent's environment while it is running.

## Ownership

Every harness-managed effect has exactly one owning `Scope`. A scope owns:

- cleanup callbacks (the inverse of an operation, registered by a registry);
- entered (async) context managers;
- child scopes;
- background tasks;
- a diagnostic record for each effect it currently owns.

```python
from chassis import Scope

scope = Scope("cache")
client = scope.enter_context(open_client())          # closed when the scope closes
scope.cleanup("close client", client.close)
scope.child("indexer")                                # closed before the parent's earlier effects
scope.create_task(warm_cache(), name="warmer")        # cancelled and awaited on close
```

Plugins never register cleanups directly. They call `ctx.tools.register(...)`,
`ctx.hooks.register(...)`, `ctx.capabilities.provide(...)`, or
`ctx.create_task(...)`, and the harness registers the inverse operation on the
plugin's scope. Closing the scope reverses every registration.

If the runtime cannot answer "which scope owns this resource?", the resource is not
safely managed, and it should not be created through the harness.

## Teardown

Closing a scope is deterministic:

1. the scope stops accepting new work;
2. owned tasks are cancelled and awaited, under a configurable timeout;
3. effects are unwound in reverse registration order;
4. failures are aggregated into a single `EffectCleanupError`.

A failing disposer never aborts the unwind: each failure is recorded and the
remaining disposers still run. A task that refuses to stop is reported rather than
silently leaking, and a task that dies with an exception is reported rather than
having its failure disappear.

`aclose()` is idempotent and safe under concurrent callers: the first caller
creates the close task and every caller awaits it. Cancelling a caller does not
abort an in-progress close.

## Plugin lifecycle

```text
PENDING ──▶ LOADING ──▶ ACTIVE ──▶ UNLOADING ──▶ DISPOSED
               │
               └──▶ FAILED
```

Lifecycle describes *ownership* state. Health describes *operational quality*:
`ACTIVE + HEALTHY`, `ACTIVE + DEGRADED`, `ACTIVE + UNHEALTHY`. A transient upstream
outage degrades a plugin; it does not destroy and recreate it.

Illegal transitions raise `HarnessStateError` rather than corrupting state.

### Setup rollback

If `setup` raises, the partially constructed scope is closed and the instance is
marked `FAILED`. No registration, task, or resource created during the failed setup
survives, and no partially configured instance becomes visible in a generation.

If a *candidate mount* fails while composing a generation, everything that attempt
mounted is rolled back and the previously published generation remains current, so
no run can observe a partial composition.

## Capabilities and reactivity

Plugins declare what they provide and require:

```python
@plugin(name="memory", version="1.0.0", provides={"memory": "1.0.0"}, requires={"database": ">=1,<2"})
async def memory(ctx: PluginContext) -> None:
    ctx.capabilities.provide(MEMORY, Store(ctx.require(DATABASE)))
```

Resolution is a greatest fixpoint over the desired plugins and the providers that
are actually available. Two rules make it well-defined:

1. a plugin cannot satisfy its own requirement;
2. active instances contribute what they *registered*, not what their manifest claims.

Removing a provider therefore removes its consumers too, in dependency-safe order,
and restoring it reactivates the same plugins. Nothing in configuration decides
this order; declared capabilities do.

Ambiguity is diagnosed, not resolved arbitrarily: two providers satisfying one
requirement make the consumer `PENDING` until a preference is set with
`prefer_provider` or `provider_preferences` in configuration.

## Generations

A `RuntimeGeneration` is an immutable view of composition, published atomically.
Runs acquire a generation and keep it:

```python
async with harness.acquire() as generation:
    provider = generation.snapshot.require(DATABASE)
```

- The snapshot is deeply immutable and is never mutated.
- Acquisition, publication, and release are synchronous sequences with no `await`
  between read and write, so a run can never observe a half-published generation
  nor lease one that is retiring.
- The data plane takes no lock. Composition is serialized on the control plane,
  and a run acquires while a mount is still in progress.

## Logical unload is not physical disposal

Removing a plugin affects the *next* generation. It does not destroy the instance:

```text
generation 17: model, search, postgres      run A acquires 17
                ↓ publish
generation 18: model, postgres              run B acquires 18
                                            run A keeps using 17
generation 17 → DRAINING
                ↓ run A finishes, leases reach 0
generation 17 → RETIRED
                ↓ search is now unreachable
search.scope.aclose()
```

The invariant is:

> A plugin scope may be physically disposed only when no live runtime generation can reach it.

Shared instances are covered by the same rule: a plugin referenced by several live
generations survives until all of them retire. Liveness is tracked independently of
the bounded generation history kept for diagnostics
(`Harness(generation_history_limit=...)`): that buffer only evicts *retired*
generations, so a run holding an old generation keeps its resources alive however
many newer generations are published.

Disposal order is derived from the providers each instance actually resolved, so
consumers are disposed before the providers they still reach.

## Reconciliation is transactional

`reconcile()` computes a plan, mounts or reuses instances, publishes a generation,
and only then disposes what left the composition. A composition-identical reconcile
reuses the current generation instead of churning one, so repeated reconciliation is
a no-op.

Programmatic `install`/`provide`/`uninstall` are synchronous desired-state changes;
`ensure_ready()` applies them at asynchronous entry points such as agent invocation.

## Shutdown

`stop()` is graceful and idempotent:

1. stop accepting new runs;
2. mark the current generation draining;
3. wait for active runs, bounded by `shutdown_grace_seconds`;
4. retire generations that are still busy, reporting the timeout as a failure;
5. dispose every remaining instance, consumers first;
6. close the harness scope;
7. raise one aggregated `EffectCleanupError` if anything failed.

## Diagnostics

```python
harness.diagnostics.plugins()       # state, health, eligibility, per-requirement resolution
harness.diagnostics.capabilities()  # registered providers
harness.diagnostics.dependencies()  # edges, activation order, pending, cycles
harness.diagnostics.generations()   # state, leases, instances, plugins
harness.diagnostics.tools()         # owner, policy
harness.diagnostics.hooks()         # owner, mode, ordering
harness.diagnostics.agents()        # registered runtimes
harness.diagnostics.desired_state() # drift against the applied configuration
harness.diagnostics.status()        # summary
harness.diagnostics.explain("memory")  # why one plugin is active, pending, or excluded
```

Diagnostics are generated from authoritative state, never scraped from logs, and
they describe configuration by key rather than by value.
