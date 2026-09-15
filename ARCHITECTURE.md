# ARCHITECTURE.md --- Agent Harness Architecture

**Status:** Architecture baseline\
**Audience:** maintainers, contributors, coding agents, plugin authors\
**Normative companion:** `SPEC.md`

## Project Conventions

The framework is named **Chassis**.

The source tree uses the canonical Python namespace:

```text
src/
  chassis/
    core/
    capabilities/
    plugins/
    hooks/
    tasks/
    tools/
    policy/
    budget/
    secrets/
    langgraph/
    telemetry/
    persistence/
    replay/
    config/
    testing/
```

All architectural documentation should refer to the runtime as **Chassis** rather than using "the harness" as a temporary project name where a proper noun is appropriate.

"Harness" may still be used as the architectural category of software that Chassis implements.

For example:

> Chassis is a production-grade agent harness.

The initial top-level runtime class MAY remain:

```python
Harness
```

if implementation experience shows that this is the clearest API:

```python
from chassis import Harness

async with Harness(...) as app:
    ...
```

The package name and product identity remain `chassis`.

A future rename from `Harness` to `Chassis` MUST NOT be performed merely for branding consistency; public API naming should optimize for clarity.

## ADR-009 — uv is the canonical project manager

**Decision:** Chassis uses uv for dependency management, environment synchronization, locking, and execution of development tooling.

The canonical dependency state is:

```text
pyproject.toml
      +
   uv.lock
```

The standard development environment is created with:

```bash
uv sync
```

Project commands execute through:

```bash
uv run <command>
```

Examples:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

### Rationale

Chassis should have one reproducible and documented Python development workflow.

Using a single project manager avoids maintaining parallel dependency-management instructions and reduces differences between:

- local development;
- coding-agent environments;
- CI;
- contributor environments.

### Consequences

- `uv.lock` MUST be committed.
- CI MUST use uv.
- Contributor documentation MUST use uv.
- Coding agents MUST use uv when installing or changing dependencies.
- Dependency changes MUST update both `pyproject.toml` and `uv.lock` as appropriate.
- Direct `pip install` commands SHOULD NOT be used as the normal project workflow.
- Poetry/PDM/Pipenv-specific project metadata MUST NOT be introduced.

## 1. Architectural thesis

The harness is a **composition and lifecycle runtime** for agents.

It does not compete with LangGraph on graph execution. It creates a
coherent runtime environment and hands an immutable view of that
environment to an execution engine.

The central separation is:

``` text
Cordis-inspired layer
    = composition + lifecycle + ownership

LangGraph
    = agent execution + graph state + durability

LangSmith
    = tracing + evaluation
```

The most important consistency boundary is:

``` text
mutable desired/runtime composition
            |
            v
     RuntimeGeneration
            |
            v
immutable view observed by an agent run
```

A runtime mutation never edits an active run's environment in place.

------------------------------------------------------------------------

## 2. Design principles

### 2.1 Ownership before cleanup

Every effect must have an owner.

If the runtime cannot answer "which scope owns this resource?", the
resource is not safely managed.

### 2.2 Logical removal is not physical disposal

Removing a plugin from desired state affects new runtime generations.

Physical disposal occurs only after no live generation can reach the
plugin instance.

### 2.3 Snapshot consistency for runs

An agent run observes one coherent runtime generation for its defined
lifetime.

Runtime reconfiguration produces a new generation instead of mutating
the old one.

### 2.4 Reactive composition

Plugins declare required capabilities rather than relying on
configuration order.

Capability availability determines eligibility for activation.

### 2.5 Prefer upstream primitives

Use LangGraph for graph execution and durability.

Use `langchain-core` for standard model/tool/Runnable interoperability.

Use LangSmith for tracing/evaluation.

The harness should add behavior only where its composition/lifecycle
responsibilities require it.

### 2.6 Explicit beats magical

Avoid hidden dependency injection, implicit globals, monkey-patching,
import-time registration, and private upstream APIs.

------------------------------------------------------------------------

## 3. System boundaries

``` text
+------------------------------------------------------------------+
| Application / Product                                            |
|                                                                  |
|  install plugins | configure agents | invoke agents | diagnostics |
+------------------------------+-----------------------------------+
                               |
                               v
+------------------------------------------------------------------+
| Harness Control Plane                                            |
|                                                                  |
| Desired State -> Resolver -> Reconciler -> Generation Publisher  |
|                       |                     |                    |
|                       +-> Plugin Lifecycle   +-> Diagnostics      |
+------------------------------+-----------------------------------+
                               |
                         atomic generation
                          publication
                               |
                               v
+------------------------------------------------------------------+
| Harness Data Plane                                               |
|                                                                  |
| acquire generation -> run snapshot -> AgentRuntime               |
|                               |                                  |
|                               +-> Tool/Policy/Budget boundaries  |
+------------------------------+-----------------------------------+
                               |
                               v
+------------------------------------------------------------------+
| LangGraph                                                        |
| StateGraph | Runtime Context | Checkpointer | Store | Interrupts  |
+------------------------------+-----------------------------------+
                               |
                               v
+------------------------------------------------------------------+
| LangSmith / OpenTelemetry                                        |
+------------------------------------------------------------------+
```

The control plane may mutate composition.

The data plane should primarily read immutable generation state.

------------------------------------------------------------------------

## 4. Major components

### 4.1 Harness

Top-level application runtime.

Responsibilities:

-   installed plugin definitions;
-   desired state;
-   lifecycle startup/shutdown;
-   reconciliation;
-   generation publication;
-   agent registry;
-   diagnostics;
-   control-plane synchronization.

It should not directly implement graph execution.

### 4.2 PluginRegistry

Tracks installed plugin definitions and active plugin instances.

It is not the dependency resolver.

It records identity, manifest, config, lifecycle, scope, and provider
registrations.

### 4.3 CapabilityRegistry

Tracks capability providers in a candidate/runtime composition.

It supports:

-   capability lookup;
-   API version matching;
-   provider identity;
-   metadata;
-   immutable snapshot creation.

The mutable registry belongs to composition/reconciliation.

Agent runs receive an immutable `CapabilitySnapshot`.

### 4.4 DependencyResolver

Computes a valid composition from:

-   desired plugin instances;
-   manifests;
-   available/provided capabilities;
-   version requirements.

Outputs should include:

-   eligible plugins;
-   pending plugins;
-   dependency edges;
-   selected providers;
-   activation order;
-   teardown order;
-   diagnostics.

### 4.5 Scope

Owns reversible effects for one plugin instance or another lifecycle
unit.

Backed primarily by `AsyncExitStack`.

### 4.6 RuntimeGeneration

Immutable published composition.

Contains or references:

-   generation ID;
-   capability snapshot;
-   plugin-instance references;
-   runtime snapshot metadata;
-   agent runtime definitions/cache keys;
-   generation state;
-   lease count/accounting.

### 4.7 GenerationManager

Coordinates:

-   current generation;
-   atomic publication;
-   generation acquisition;
-   lease release;
-   draining;
-   garbage collection/disposal of unreachable plugin instances.

### 4.8 AgentRuntime

Minimal execution boundary.

Initial implementation: `LangGraphAgentRuntime`.

### 4.9 ToolExecutor

Harness-owned boundary around tool calls where policy, approval, budget,
timeout, tracing, and normalized errors may be enforced.

### 4.10 Telemetry integration

Adds lifecycle/control-plane visibility while allowing
LangGraph/LangChain native tracing to handle graph/model/tool internals.

------------------------------------------------------------------------

## 5. Plugin identity vs plugin instance

A plugin definition and a mounted plugin instance are distinct.

Example:

``` text
Plugin definition:
  type = postgres
  version = 2.1.0

Desired entry:
  id = analytics-db
  plugin = postgres
  config = {...}

Mounted instance:
  instance_id = plugin_8f...
  scope = scope_a1...
```

Stable desired-state IDs are required so reconciliation can distinguish:

-   unchanged;
-   reconfigured;
-   replaced;
-   removed;
-   newly added.

A plugin instance has exactly one owning scope.

A plugin instance may be referenced by multiple runtime generations.

------------------------------------------------------------------------

## 6. Scope and effect ownership

Conceptual API:

``` python
class Scope:
    async def enter_async_context(self, cm): ...
    def callback(self, fn, *args, **kwargs): ...
    def create_task(self, coro): ...
    async def aclose(self): ...
```

Internally:

``` text
Scope
 |
 +-- AsyncExitStack
 |
 +-- Task ownership
 |
 +-- child scopes
 |
 +-- diagnostic effect records
```

A registry operation such as:

``` python
ctx.tools.register(tool)
```

performs:

``` text
register tool
    |
    +-> record owner = current scope
    |
    +-> register inverse/unregister callback
```

Therefore closing the scope reverses the registration.

### Cleanup order

Default:

``` text
stop new scope work
    |
cancel/finish owned tasks
    |
reverse-order effect cleanup
    |
aggregate cleanup failures
    |
CLOSED
```

Sequential reverse-order cleanup is the default because dependencies
between acquired resources are common.

Parallel cleanup is opt-in only where independence is known.

------------------------------------------------------------------------

## 7. Plugin lifecycle state machine

``` text
                 dependency satisfied
        +-----------------------------------+
        |                                   v
     PENDING ---------------------------> LOADING
        ^                                   |
        |                                   | setup success
        |                                   v
        |                                ACTIVE
        |                                   |
        | dependency unavailable            | reconciliation removes
        +-----------------------------------+ or invalidates
                                            |
                                            v
                                        UNLOADING
                                            |
                                            v
                                         DISPOSED

LOADING -- setup exception --> FAILED
```

Important nuance: dependency disappearance in desired/new composition
does not imply immediate destruction of an instance still reachable from
an old generation.

Thus there are two related notions:

1.  **composition eligibility** --- whether the plugin belongs in a
    newly published generation;
2.  **physical lifetime** --- whether its scope may be disposed.

They must not be conflated.

------------------------------------------------------------------------

## 8. Dependency graph

Given:

``` text
PostgresPlugin
    provides DATABASE

MemoryPlugin
    requires DATABASE
    provides MEMORY

AgentExtensionPlugin
    requires MEMORY
```

the dependency graph is:

``` text
PostgresPlugin
      |
      v
MemoryPlugin
      |
      v
AgentExtensionPlugin
```

The resolver should work from capability requirements, not direct plugin
names.

A plugin may require:

``` text
database >=1,<2
```

and multiple providers may potentially satisfy it.

Provider selection policy must be deterministic.

Initial implementation may require exactly one unambiguous provider
unless configuration explicitly selects among candidates.

Ambiguous provider selection should produce a diagnostic rather than
nondeterministic behavior.

------------------------------------------------------------------------

## 9. Dependency resolution algorithm

A practical initial algorithm:

1.  Parse and validate all manifests.
2.  Index candidate providers by capability key.
3.  For each required capability:
    -   find compatible providers;
    -   apply explicit provider selection if configured;
    -   reject ambiguity if no deterministic rule resolves it.
4.  Build plugin dependency edges from selected providers to consumers.
5.  Detect cycles.
6.  Topologically order activation.
7.  Mark plugins with unsatisfied requirements as `PENDING`.
8.  Propagate unsatisfied state to downstream consumers.
9.  Produce a resolved composition and diagnostics.

For teardown, reverse the dependency-safe activation order for instances
that actually become unreachable.

The resolver must be deterministic for the same canonical desired state.

------------------------------------------------------------------------

## 10. Why runtime generations exist

Without generations:

``` text
Run A -> model A
      -> tool X

             config changes

Run A -> model B ?
      -> tool X removed ?
```

The run can observe an incoherent environment.

With generations:

``` text
                 publish
Generation 17 -------------> Generation 18

model A                      model B
tool X                       tool X

Run A ---------------------> stays on 17
Run B ---------------------------------> uses 18
```

This is conceptually similar to snapshot/MVCC-style consistency.

The generation is the consistency boundary, not necessarily the physical
ownership boundary.

------------------------------------------------------------------------

## 11. Generation state machine

``` text
BUILDING
   |
   | validation success
   v
ACTIVE
   |
   | newer generation published
   v
DRAINING
   |
   | lease count == 0
   v
RETIRED
```

A failed `BUILDING` generation is discarded and never published.

Only one generation is current/active for new acquisitions at a time,
unless future routing explicitly supports multiple active variants.

------------------------------------------------------------------------

## 12. Generation acquisition

Conceptually:

``` python
async with generations.acquire() as generation:
    run_context = generation.make_run_context(...)
    return await agent_runtime.invoke(request, run_context)
```

Acquisition must atomically:

1.  identify the current generation;
2.  increment/acquire its lease;
3.  return it.

Release decrements the lease.

Publication and acquisition must be synchronized sufficiently to prevent
acquiring a half-retired/partially published generation.

A small control-plane lock is acceptable.

A global lock around every model/tool call is not.

------------------------------------------------------------------------

## 13. Safe unload and physical disposal

Suppose:

``` text
Generation 17
  SearchPlugin
  PostgresPlugin

Generation 18
  PostgresPlugin
```

and Generation 17 still has active runs.

Logical unload of SearchPlugin has already happened for new work because
Generation 18 does not reference it.

Physical disposal waits:

``` text
SearchPlugin
   |
   +-- reachable by Generation 17
            |
            +-- lease count > 0
```

When Generation 17 reaches zero leases and retires:

``` text
Generation 17 reference removed
        |
        v
SearchPlugin refcount/reachability -> 0
        |
        v
SearchPlugin.scope.aclose()
```

Postgres is shared:

``` text
                PostgresPlugin
                  /       \
                 /         \
        Generation 17   Generation 18
```

Retiring Generation 17 must not dispose Postgres because Generation 18
still reaches it.

Core invariant:

``` text
physical_dispose(plugin)
only if
reachable_from_live_generations(plugin) == false
```

------------------------------------------------------------------------

## 14. Reconciliation transaction

Desired-state mutation should behave transactionally from the
perspective of new runs.

``` text
Desired State N
      |
      | config mutation
      v
Desired State N+1
      |
      v
resolve candidate graph
      |
      v
mount/reuse candidate plugin instances
      |
      v
validate candidate generation
      |
      +-- failure --> rollback candidate scopes/effects
      |
      v
atomic publish
      |
      v
old generation -> DRAINING
```

No run may see the candidate before successful publication.

If candidate construction fails, the current generation remains current.

Newly created candidate resources must be cleaned up.

------------------------------------------------------------------------

## 15. Reuse vs replacement of plugin instances

A plugin instance may be reused when its effective identity is
unchanged.

An effective identity may include:

-   plugin implementation/version;
-   stable desired entry ID;
-   canonical relevant config hash;
-   scope class/tenant where relevant.

If configuration changes in a way the plugin explicitly supports
in-place, future versions may support a controlled reconfigure protocol.

For the initial design, replacement with a new instance/generation is
safer than arbitrary in-place mutation.

Do not mutate shared plugin state if old generations still depend on the
previous semantics.

------------------------------------------------------------------------

## 16. Runtime-bound vs build-time graph dependencies

This distinction prevents unnecessary recompilation.

### Runtime-bound

Graph topology is stable; provider is resolved from run context.

``` text
MODEL
DATABASE
POLICY
SECRETS
tenant identity
```

A model swap:

``` text
OpenAI -> Anthropic
```

should usually mean:

``` text
new generation
same compiled graph cache entry
different runtime capability snapshot
```

### Build-time

The graph itself changes.

Examples:

``` text
state schema
node set
edges
conditional routing structure
static ToolNode contents
graph middleware topology
```

These affect `graph_definition_hash` or equivalent and invalidate the
relevant cache entry.

------------------------------------------------------------------------

## 17. Graph cache

Conceptual key:

``` text
GraphCacheKey(
    agent_definition_version,
    state_schema_hash,
    topology_hash,
    static_tool_schema_hash_if_relevant,
    middleware_topology_hash,
    relevant_build_time_plugin_versions,
)
```

Do not include unrelated runtime values.

The cache stores compiled graph objects only when upstream semantics
permit safe reuse.

Cache invalidation must be explicit and testable.

------------------------------------------------------------------------

## 18. LangGraph bridge

Use LangGraph's public runtime context facilities.

Conceptually:

``` python
@dataclass(frozen=True)
class HarnessRunContext:
    generation_id: str
    run_id: str
    capabilities: CapabilitySnapshot
    user_id: str | None = None
    tenant_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
```

A node can then conceptually do:

``` python
async def model_node(state, runtime: Runtime[HarnessRunContext]):
    model = runtime.context.capabilities.require(MODEL)
    ...
```

The exact API must follow the current installed LangGraph version.

No `CURRENT_HARNESS` global should exist.

`contextvars` may be used for narrowly scoped correlation or ergonomics,
but not as the authoritative capability store.

------------------------------------------------------------------------

## 19. AgentRuntime boundary

The core knows a small protocol:

``` python
class AgentRuntime(Protocol):
    async def invoke(self, request, run_context): ...
    def stream(self, request, run_context): ...
```

`LangGraphAgentRuntime` adapts a registered agent definition/compiled
graph to this protocol.

Resume/cancel semantics should be added only when they can be
represented honestly.

Do not pretend all future backends share identical durability semantics.

------------------------------------------------------------------------

## 20. Checkpoint and Store boundaries

``` text
LangGraph Checkpointer
    |
    +-- graph/thread execution state
    +-- durable resume
    +-- interrupts/checkpoints

LangGraph Store
    |
    +-- cross-thread / long-term agent data where appropriate

Harness persistence
    |
    +-- desired plugin config
    +-- generation metadata
    +-- runtime snapshots
    +-- deployment metadata
    +-- replay records
```

The harness must not mirror graph checkpoints into a second home-grown
checkpoint system.

------------------------------------------------------------------------

## 21. Tool execution architecture

``` text
LangGraph / Agent
       |
       v
tool request
       |
       v
Harness ToolExecutor
       |
       +--> resolve registered tool from generation snapshot
       |
       +--> policy check
       |
       +--> approval gate if required
       |
       +--> budget/deadline check
       |
       +--> tracing
       |
       v
langchain-core compatible tool
       |
       v
normalized result/error
```

A tool registry entry wraps rather than replaces
`BaseTool`/`StructuredTool`.

Policy metadata belongs to the harness.

Tool schema/name/description/execution behavior should remain
upstream-compatible.

------------------------------------------------------------------------

## 22. Hooks architecture

Hooks are for harness-owned boundaries.

Avoid duplicating LangGraph/LangChain callback systems.

A hook registration contains:

-   event key;
-   handler;
-   mode;
-   priority/order;
-   owner scope;
-   timeout/error policy where relevant.

Because registration is scope-owned:

``` text
Plugin unload
   |
   v
Scope close
   |
   v
hook registration automatically removed
```

Initial hook count should remain deliberately small.

------------------------------------------------------------------------

## 23. Task ownership and cancellation

Every plugin background task must be owned by a scope.

Desired behavior:

``` text
scope closing
    |
    v
reject new child tasks
    |
    v
cancel/request shutdown
    |
    v
await children
    |
    v
remaining resource cleanup
```

Cancellation must not be swallowed silently.

If a plugin needs graceful task shutdown, it should expose an async
context manager/resource whose exit path performs that protocol.

------------------------------------------------------------------------

## 24. Concurrency model

### Control plane

Operations such as:

-   install/uninstall;
-   desired-state mutation;
-   reconciliation;
-   generation publication;
-   shutdown;

may be serialized through an async control-plane lock.

This keeps the first implementation understandable.

### Data plane

Agent runs:

-   acquire an immutable generation;
-   operate without holding the control-plane lock;
-   release their lease on completion.

Tool/model calls should not require global composition locks.

### Publication

Publication must be atomic from the perspective of acquisition.

### Disposal

Disposal occurs after reachability/lease accounting says an instance is
unreachable.

Disposal itself must not hold locks longer than necessary. Prefer
determining disposal candidates under synchronization and performing
potentially slow async cleanup outside the critical section while
preventing resurrection.

------------------------------------------------------------------------

## 25. Race conditions explicitly addressed

### Race: acquire vs publish

A run must acquire either old or new generation, never an intermediate
state.

### Race: unload vs tool execution

The executing run holds a generation lease. The plugin remains reachable
and cannot be physically disposed.

### Race: two reconciliations

Serialize initial implementation. Later optimization may coalesce
desired-state changes.

### Race: shutdown vs new run

Shutdown flips an acceptance state under synchronization before draining
generations.

### Race: cleanup vs reused plugin

Reachability/refcount is checked after generation reference updates.
Shared instances remain alive.

------------------------------------------------------------------------

## 26. Failure model

### Plugin setup failure

``` text
LOADING
  |
  +-- effect A
  +-- effect B
  |
  X exception
  |
  v
scope closes
  |
  +-- revert B
  +-- revert A
  |
  v
FAILED
```

Candidate generation is not published.

### Cleanup failure

Continue cleanup, aggregate errors, mark diagnostics/health accordingly.

### Resolver failure

No generation publication.

Current active generation remains valid.

### Graph compile failure

Treat as candidate-generation/agent-definition validation failure. Do
not publish a generation that references an unusable required agent.

### LangSmith failure

Observability failure should normally not corrupt agent runtime
semantics. Define bounded failure behavior and avoid making tracing a
single point of failure unless explicitly configured as required.

------------------------------------------------------------------------

## 27. Health vs lifecycle

Lifecycle describes ownership state.

Health describes operational quality.

Examples:

``` text
ACTIVE + HEALTHY
ACTIVE + DEGRADED
ACTIVE + UNHEALTHY
PENDING + not-applicable
```

A transient upstream outage should not necessarily destroy/recreate a
plugin.

Health policy may later influence reconciliation, but the concepts
remain separate.

------------------------------------------------------------------------

## 28. Policy and security model

### Trusted boundary

In-process Python plugins are trusted code.

They can import Python modules and bypass harness APIs if malicious.

Therefore:

``` text
Harness Policy Engine != Plugin Sandbox
```

Policy applies to harness-mediated operations.

### Future isolation

The architecture should not prevent future plugin execution modes:

``` text
in-process
subprocess
container
remote service
```

but only in-process execution is required initially.

### Secrets

Secret providers return values only to authorized callers.

Redaction applies before:

-   telemetry;
-   snapshots;
-   diagnostics;
-   replay;
-   serialized errors.

### Temporary privilege

A future privileged capability may be represented by a short-lived
scoped provider. Its disappearance is then enforced structurally by
scope lifetime as well as by policy.

------------------------------------------------------------------------

## 29. Runtime snapshot and hashing

A runtime snapshot is immutable descriptive metadata, not a live object
graph.

Canonicalization rules must be documented.

A possible approach:

1.  construct JSON-compatible data;
2.  sort object keys;
3.  normalize versions/identifiers;
4.  exclude volatile fields;
5.  exclude secrets;
6.  serialize with fixed separators/encoding;
7.  hash with a documented cryptographic hash.

Separate hashes should exist for different concerns:

``` text
config_hash
plugin_graph_hash
graph_definition_hash
tool_schema_hash
prompt_hash
```

Do not create one giant hash whose invalidation semantics are impossible
to understand.

------------------------------------------------------------------------

## 30. LangSmith architecture

Use native LangGraph/LangChain tracing for execution.

Add harness spans around:

``` text
harness.reconcile
plugin.mount
plugin.unmount
dependency.resolve
generation.build
generation.publish
generation.drain
policy.decision
budget.exhausted
graph.compile
graph.cache
```

Trace metadata may include:

``` text
generation_id
harness_version
plugin_graph_hash
plugin_versions
capability_versions
graph_definition_hash
```

Do not attach unrestricted secret-bearing config objects.

OpenTelemetry can be used for interoperability/fan-out where supported.

------------------------------------------------------------------------

## 31. Replay architecture

Replay sits at explicit harness boundaries.

``` text
record mode

model boundary ----> recorder
tool boundary -----> recorder
interrupt ----------> recorder
snapshot -----------> recorder
```

In replay mode, supported boundaries may be replaced by recorded
providers/results.

The recorder must include enough identity data to detect mismatch rather
than silently returning the wrong recording.

Example mismatch dimensions:

-   operation type;
-   tool name;
-   canonical input hash;
-   model/request identity;
-   sequence/correlation ID.

Unsupported external effects fail explicitly or execute live only if the
replay policy permits it.

------------------------------------------------------------------------

## 32. Desired-state reconciliation

Desired state:

``` yaml
plugins:
  - id: primary-model
    plugin: openai-model
    config: {...}
  - id: search
    plugin: web-search
```

Actual state contains mounted instances and current generations.

The reconciler computes a diff:

``` text
UNCHANGED
ADD
REMOVE
REPLACE
RECONFIGURE
```

For v0.1, `RECONFIGURE` may conservatively become `REPLACE`.

Process:

``` text
load desired config
      |
validate
      |
diff actual
      |
resolve candidate dependencies
      |
mount new/replacement instances
      |
build candidate generation
      |
publish atomically
      |
drain old
```

------------------------------------------------------------------------

## 33. Diagnostics architecture

Diagnostics should be generated from authoritative runtime state, not
scraped logs.

For a pending plugin:

``` text
Plugin: memory
State: PENDING

Requires:
  database >=1,<2

Resolution:
  no compatible provider

Candidates:
  postgres-db provides database 2.0  [incompatible]
```

For a generation:

``` text
Generation: gen_018
State: DRAINING
Leases: 3
Referenced plugins:
  model/openai#...
  search#...
  postgres#...
```

Diagnostics must redact secrets.

------------------------------------------------------------------------

## 34. Suggested package dependency direction

``` text
core
 ^
 |
capabilities   plugins
 ^              ^
 |              |
 +------ runtime/generation
             ^
             |
      tools / hooks / policy
             ^
             |
         langgraph
             ^
             |
        application
```

More concretely:

-   `core` must not import LangGraph.
-   `plugins` and `capabilities` must not depend on LangSmith.
-   `langgraph` may depend on core/capabilities/tools.
-   `telemetry.langsmith` may depend on the telemetry protocol and
    LangSmith.
-   application/config layers may assemble everything.

Circular imports are a design smell and should be prevented by
protocols/data types in lower-level packages.

------------------------------------------------------------------------

## 35. Proposed repository layout

``` text
src/
  harness/
    core/
      runtime.py
      context.py
      generation.py
      scope.py
      effects.py
      errors.py

    capabilities/
      keys.py
      registry.py
      snapshot.py
      versions.py

    plugins/
      base.py
      manifest.py
      registry.py
      resolver.py
      loader.py

    hooks/
      types.py
      registry.py

    tasks/
      manager.py

    tools/
      registry.py
      metadata.py
      executor.py

    policy/
      engine.py
      permissions.py

    budget/
      models.py
      governor.py

    secrets/
      base.py
      env.py

    langgraph/
      runtime.py
      context.py
      graphs.py
      cache.py

    telemetry/
      base.py
      langsmith.py

    persistence/
      runtime_store.py
      snapshots.py

    replay/
      models.py
      recorder.py
      player.py

    config/
      models.py
      loader.py
      reconcile.py

    testing/
      harness.py
      fakes.py

tests/
examples/
docs/
```

This is a starting point, not a requirement to create empty packages.

------------------------------------------------------------------------

## 36. Public API philosophy

Optimize for plugin authors and application authors, not framework
internals.

Application:

``` python
async with Harness(config) as app:
    result = await app.agents.invoke(
        "research-agent",
        {"messages": [...]},
    )
```

Plugin:

``` python
@plugin(
    name="search",
    version="1.0.0",
    requires={"tools": ">=1,<2"},
)
async def search(ctx):
    ctx.tools.register(search_tool)
```

The framework should hide:

-   cleanup stacks;
-   refcounts;
-   resolver graph mutation;
-   generation publication locks;
-   hook deregistration;
-   trace correlation.

Diagnostics should make these internals observable without requiring
users to manipulate them.

------------------------------------------------------------------------

## 37. Architecture invariants

These invariants are normative.

### I1 --- Effect ownership

Every harness-managed effect has exactly one owning scope.

### I2 --- Setup rollback

If plugin setup fails, all effects created by that setup are reverted.

### I3 --- Dependency coherence

An active composition never contains a plugin whose required selected
capability provider is absent/incompatible.

### I4 --- Generation immutability

A published generation is never mutated in a way visible to existing
runs.

### I5 --- Atomic publication

A run acquires either the old complete generation or the new complete
generation.

### I6 --- Safe disposal

A plugin scope is never physically disposed while reachable from a live
generation.

### I7 --- Shared lifetime

A plugin referenced by multiple generations survives until all
referencing live generations retire.

### I8 --- No data-plane global lock

Normal model/tool execution does not require the control-plane
reconciliation lock.

### I9 --- Upstream ownership

LangGraph owns graph durability; the harness does not duplicate it.

### I10 --- Security honesty

In-process plugin code is trusted; policy is not represented as
sandboxing.

### I11 --- Secret non-observability

Secret values do not enter snapshots, diagnostics, replay, traces, or
serialized public errors.

### I12 --- Deterministic resolution

Equivalent canonical desired state produces equivalent
provider/dependency resolution.

------------------------------------------------------------------------

## 38. Architectural decisions and tradeoffs

### ADR-001 --- LangGraph is an execution backend, not the harness kernel

**Decision:** keep core independent of LangGraph.

**Why:** lifecycle/composition concerns are useful even outside a graph
engine and should not be encoded as graph nodes.

**Tradeoff:** an integration layer is required.

### ADR-002 --- Use immutable generations instead of in-place hot mutation

**Decision:** reconfiguration publishes a new generation.

**Why:** prevents active runs from seeing incoherent environments and
makes unload safe.

**Tradeoff:** temporarily retains old resources while runs drain.

### ADR-003 --- Use `AsyncExitStack` for reversible effects

**Decision:** Python-native scoped cleanup is the default implementation
mechanism.

**Why:** deterministic, idiomatic, async-aware.

**Tradeoff:** does not reproduce Cordis internals exactly; only the
semantic guarantees are adopted.

### ADR-004 --- Prefer LangChain Core model/tool interfaces

**Decision:** wrap with metadata rather than replace.

**Why:** ecosystem compatibility and native LangGraph/LangSmith
behavior.

**Tradeoff:** some harness concerns require an execution wrapper around
tools.

### ADR-005 --- Separate build-time and runtime-bound capabilities

**Decision:** graph recompilation depends only on build-time inputs.

**Why:** provider swaps should be cheap and should not invalidate
unrelated graph caches.

**Tradeoff:** agent definitions must declare/derive dependency class
correctly.

### ADR-006 --- Serialize initial control-plane reconciliation

**Decision:** use a straightforward async control-plane synchronization
strategy first.

**Why:** correctness is more valuable than concurrent reconciliation
throughput initially.

**Tradeoff:** configuration mutations are serialized.

### ADR-007 --- No production Python HMR in v0.1

**Decision:** support runtime provider/config replacement, not arbitrary
module identity replacement.

**Why:** Python HMR introduces difficult class/module/state identity
problems unrelated to core value.

### ADR-008 --- Replay is boundary-based, not universally deterministic

**Decision:** record explicit model/tool/interrupt boundaries.

**Why:** honest and implementable.

**Tradeoff:** arbitrary external side effects are not automatically
replayable.

------------------------------------------------------------------------

## 39. Evolution paths

The architecture intentionally leaves room for:

-   subprocess/container/remote plugin isolation;
-   distributed desired-state control plane;
-   tenant-scoped provider pools;
-   dynamic short-lived privileged capability scopes;
-   alternative `AgentRuntime` backends;
-   richer health-based reconciliation;
-   plugin catalogs;
-   MCP capability providers;
-   self-composing agents;
-   code-development hot reload as a non-production feature.

These features must not weaken the core invariants.

------------------------------------------------------------------------

## 40. Implementation order

The architecture must be implemented bottom-up:

``` text
Scope / effects
      |
      v
Capabilities / plugin lifecycle
      |
      v
Dependency resolver
      |
      v
Runtime generations / leases / draining
      |
      v
Tools / hooks / policy / tasks
      |
      v
LangGraph integration
      |
      v
LangSmith / snapshots
      |
      v
Config / reconciliation
      |
      v
Replay / evals
```

Do not integrate LangGraph before lifecycle and generation semantics are
demonstrated by tests.

------------------------------------------------------------------------

## 41. Review checklist

Before calling the architecture production-ready, review:

-   Can any plugin resource outlive its scope accidentally?
-   Can setup failure leak a registration?
-   Can an old run observe a new provider?
-   Can a plugin be disposed while a tool call uses it?
-   Can two generations share a plugin safely?
-   Can publication expose a partial candidate?
-   Can a dependency cascade unload in the wrong order?
-   Can ambiguous providers resolve nondeterministically?
-   Can graph cache keys over-invalidate?
-   Is any LangGraph functionality unnecessarily duplicated?
-   Is any `langchain-core` abstraction unnecessarily reinvented?
-   Can a plugin create an unowned background task?
-   Can secrets enter telemetry or snapshots?
-   Can tracing failure break runtime correctness?
-   Can shutdown deadlock with generation draining?
-   Are cleanup errors aggregated?
-   Are public diagnostics sufficient to explain pending plugins?
-   Are concurrency invariants covered by tests?

The project should not advance to more sophisticated features until
these questions have strong answers.

