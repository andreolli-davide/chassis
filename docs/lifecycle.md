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
remaining disposers still run — including when a disposer raises something
outside `Exception`, which is aggregated too and chained as the cause of the
raised `EffectCleanupError` instead of masking the other failures. A task that
refuses to stop is reported rather than silently leaking, and a task that dies
with an exception is reported rather than having its failure disappear.

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

## Incremental reuse

A reconcile does not rebuild the whole composition. Each eligible entry's *semantic
identity* is recomputed and compared with the identity its instance was mounted
with; when they are equal, the exact same instance is carried into the new
generation. A change rebuilds only the nodes whose semantic inputs changed, plus the
consumers that reach them. See
[incremental-composition.md](incremental-composition.md) for the model and
`harness.diagnostics.analyze_impact(old, new)` for the analysis.

Reuse is sharing, never shared mutability: a reused instance is never reconfigured
in place, so a published generation still never observes a composition change after
publication. A change that would require in-place mutation rebuilds the node.

## Generation pressure

Draining for a long time is correct but not free: while a run holds a lease, the
generation keeps its plugin instances alive and the resources behind them. Chassis
does not guess whether that is too long — it reports it.

```python
report = harness.diagnostics.generation_pressure()
print(report.to_text())
```

```text
current_generation: gen_00a2
live_generations: 4
draining_generations: 3
oldest_lease_age_seconds: 862

gen_009f
  state: draining
  age_seconds: 912
  leases: 1
  oldest_lease_age_seconds: 862
  retained_plugins:
    - postgres-primary (postgres)
```

`GenerationPressureReport` is structured data; `to_dict()` gives a JSON-compatible
form and `metrics()` gives vendor-neutral gauges for a telemetry backend:

```python
report.live_generations           # how many generations are still live
report.draining_generations       # how many are waiting for their last lease
report.oldest_lease_age_seconds   # age of the oldest outstanding lease, if any
report.generations                # per-generation age, state, leases, retained plugins
report.instance_generations       # instance id -> live generations that reach it
report.resources                  # per-resource reachability and why it is retained

harness.diagnostics.instance_generations(instance.instance_id)  # newest first
```

`report.resources` answers "who keeps this resource alive?" for each reachable
instance, including resources shared by several generations after an incremental
reconcile:

```python
resource = next(item for item in report.resources if item.entry_id == "postgres")
resource.generations     # ("gen_0044", "gen_0043") — newest first
resource.retained_by     # ("lease", "sharing")
```

`lease` means a run still holds a generation that reaches the resource; `sharing`
means more than one live generation reaches it, so no single generation's
retirement would release it. A resource with no reaching generation is absent
because it is about to be disposed, not because its reachability is unknown.

Four different things are easy to conflate, and the report keeps them apart:

| Concept | Meaning | Where it lives |
| --- | --- | --- |
| Diagnostic history | Retired generations kept for *diagnostics* only, bounded by `generation_history_limit` | `GenerationManager.history` |
| Liveness | Generations a run can still reach: current + draining | `GenerationManager.live()` |
| Lease | One run's hold on one generation, with a start time | `GenerationAccounting`, `GenerationLease` |
| Physical disposal | Closing the scope of an instance no live generation reaches | `Harness._reclaim` |

The bounded history never decides liveness: it only evicts *retired* generations, so
a leased generation survives any number of newer publications and its plugin
instances stay alive. `report.history_limit`, `report.history_retained`, and
`report.history_evicted` make the difference visible.

Pressure is observability, not enforcement. Chassis adds no default limit and
reclaims nothing because it is old: a loitering generation is reported so the
operator can find the run keeping it alive (see
[troubleshooting.md](troubleshooting.md#an-old-generation-is-still-live)).

## Reconciliation is transactional

`reconcile()` computes a plan, mounts or reuses instances, publishes a generation,
and only then disposes what left the composition. A composition-identical reconcile
reuses the current generation instead of churning one, so repeated reconciliation is
a no-op.

Programmatic `install`/`provide`/`uninstall` are synchronous desired-state changes;
`ensure_ready()` applies them at asynchronous entry points such as agent invocation.

## Shutdown

`stop()` is graceful, idempotent, and a **barrier**: every caller blocks until
the shutdown has actually finished and then observes its result, so no caller
is told the harness is stopped while disposal is still running. Cancelling one
caller does not abort the shutdown — it completes in the background, and a
later `stop()` observes the same result.

1. stop accepting new runs (`acquire()` refuses a stopping harness);
2. mark the current generation draining;
3. wait for active runs, bounded by `shutdown_grace_seconds` — one deadline for
   every draining generation together, not one grace per generation;
4. retire generations that are still busy, reporting the timeout as a failure;
5. dispose every remaining instance, consumers first;
6. close the harness scope;
7. raise one aggregated `EffectCleanupError` if anything failed.

Shutdown is **terminal**: a stopped harness is never started again (build a new
one), so its terminal state is deterministic instead of a half-restartable
process. A cancelled or failing shutdown still reaches the terminal state, and
its failure is recorded and re-raised to every `stop()` caller — the harness is
never left wedged in `STOPPING`.

Owned tasks that resist cancellation past the shutdown timeout are reported as
failures and stay visible afterwards (`scope.stragglers`), and such a scope is
never presented as fully disposed (`scope.fully_disposed` is `False`): Chassis
does not pretend that work which is still running has been cleaned up. The same
applies to a scope whose cleanup failed or whose unwind was interrupted —
`fully_disposed` requires every effect released and zero recorded failures, and
the failures stay visible after close instead of being swallowed into the
raised error. The configured `Harness(task_shutdown_timeout=...)` applies to
plugin scopes as well as the harness scope.

## Proving resources returned to baseline

`harness.diagnostics.resource_counts()` aggregates the counters that matter for
lifecycle correctness — instances, owned scopes, effects, tasks, stragglers,
cleanup failures, leases, and live/draining generations — from authoritative
state with no lock and no `await`, so a run or a stress cycle can be bracketed
by two reads:

```python
before = harness.diagnostics.resource_counts()
# ... start, run, stop ...
assert harness.diagnostics.resource_counts().to_dict() == before.to_dict()
```

Everything it counts except desired `entries` (which stay installed) must
return to its pre-run value once the harness has stopped. The stress and soak
suites use exactly this check.

## Diagnostics

```python
harness.diagnostics.plugins()       # state, health, eligibility, per-requirement resolution, owned effects
harness.diagnostics.capabilities()  # registered providers
harness.diagnostics.dependencies()  # edges, activation order, pending, cycles
harness.diagnostics.generations()   # state, leases, instances, plugins
harness.diagnostics.generation_pressure()     # liveness, lease age, retained work
harness.diagnostics.resource_counts()         # instances, scopes, effects, tasks, leases
harness.diagnostics.instance_generations(id)  # live generations reaching one instance
harness.diagnostics.budgets()       # default limits and their enforcement modes
harness.diagnostics.tools()         # owner, policy
harness.diagnostics.hooks()         # owner, mode, ordering
harness.diagnostics.agents()        # registered runtimes
harness.diagnostics.desired_state() # drift against the applied configuration
harness.diagnostics.status()        # summary
harness.diagnostics.explain("memory")  # why one plugin is active, pending, or excluded
harness.diagnostics.scopes()        # the resolved scope tree of the current generation
harness.diagnostics.explain_requirement("agent", "database")  # provenance of one requirement
harness.diagnostics.explain_scope("/research")                # visibility and ownership of one scope
harness.diagnostics.diff_generations("gen_0004", "gen_0005")  # semantic composition diff
harness.diagnostics.analyze_impact("gen_0004", "gen_0005")    # reuse/rebuild analysis
harness.diagnostics.explain_reuse("gen_0004", "gen_0005", "search")  # why one node was reused or rebuilt
harness.diagnostics.explain_agent("finance", revision="17")  # composition of one agent revision
harness.diagnostics.diff_agents("finance", "17", "18")       # what changed between revisions
```

Scoped composition is described in [scopes.md](scopes.md): hierarchy, inheritance,
capability narrowing, provenance, and the explain and diff APIs.
Agent composition is described in [agent-composition.md](agent-composition.md):
`AgentSpec`, immutable revisions, materialization, pinning, and retirement.

Diagnostics are generated from authoritative state, never scraped from logs, and
they describe configuration by key rather than by value. Effect descriptions are
redacted before they are reported, so diagnostics never become somewhere a secret
accumulates.
