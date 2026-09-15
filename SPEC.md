# SPEC.md --- Production-Grade Python Agent Harness

**Status:** Draft specification\
**Target:** v0.1 foundation, followed by incremental production
hardening\
**Language:** Python \>= 3.12

## Project Identity

The project is named **Chassis**.

The canonical repository name SHOULD be:

```text
chassis
```

The canonical Python package namespace MUST be:

```python
import chassis
```

Public APIs SHOULD use the Chassis name consistently.

Examples:

```python
from chassis import Harness, Plugin, CapabilityKey
from chassis.langgraph import LangGraphAgentRuntime
```

Do not introduce alternative product names, legacy aliases, or placeholder package names such as `harness`, `agent_harness`, or `runtime_framework` in public APIs.

Internal implementation modules MUST live under:

```text
src/chassis/
```

The conceptual meaning of the name is intentional:

> Chassis is the runtime structure on which agent capabilities, plugins, execution engines, policies, and resources are mounted and safely managed.

LangGraph is an execution engine mounted within Chassis; Chassis itself is not a graph framework.

## Python Project Management

Chassis MUST use **uv** as its canonical Python project and dependency manager.

Do not use Poetry, Pipenv, PDM, or Conda as alternative project-management systems.

The canonical project configuration MUST live in:

```text
pyproject.toml
```

The repository MUST commit:

```text
uv.lock
```

Dependency installation and synchronization MUST use:

```bash
uv sync
```

Commands that execute project tooling SHOULD use:

```bash
uv run ...
```

For example:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

Dependencies SHOULD be added through uv when practical:

```bash
uv add <package>
uv add --dev <package>
```

The lockfile MUST remain synchronized with `pyproject.toml`.

CI MUST use uv and the committed lockfile to create reproducible environments.

Documentation, contributor instructions, examples, and coding-agent instructions MUST use uv commands consistently.

Do not maintain parallel `requirements.txt` files unless a concrete external integration requires one.

If such a compatibility file is ever required, `pyproject.toml` and `uv.lock` remain the canonical dependency definitions.

The minimum supported Python version is:

```text
Python >= 3.12
```

A typical local development workflow SHOULD be:

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

## 1. Purpose

This project is a production-grade Python agent harness inspired by
Cordis and the principles of spatiotemporal composability.

The harness manages the runtime environment in which agents execute:
plugins, capabilities, dependency activation, scoped resources,
reversible effects, runtime generations, policy, budgets, secrets,
configuration, diagnostics, and observability metadata.

It does **not** replace LangGraph. LangGraph is the first-class
execution engine for graph execution, state transitions, checkpointing,
interrupts, resume, streaming, subgraphs, and durable execution.

It does **not** replace LangChain Core abstractions for models, tools,
and runnables when those abstractions are sufficient.

It does **not** replace LangSmith. LangSmith is the primary tracing and
evaluation integration.

The core product proposition is:

> Agents should execute against a coherent, versioned runtime
> composition whose capabilities can appear, disappear, and be replaced
> safely without leaking resources or mutating the environment
> underneath active runs.

------------------------------------------------------------------------

## 2. Goals

The harness MUST provide:

1.  A small plugin lifecycle kernel.
2.  Typed, versioned capabilities.
3.  Reactive plugin activation based on capability availability.
4.  Scoped ownership of resources and registrations.
5.  Reversible effects with deterministic cleanup.
6.  Safe dependency-aware unload.
7.  Immutable runtime generations for active-run consistency.
8.  Safe provider replacement and generation draining.
9.  LangGraph as the official first-class agent runtime.
10. Native interoperability with `langchain-core` tools, models, and
    runnables.
11. LangSmith tracing and evaluation integration.
12. Policy enforcement at harness-controlled execution boundaries.
13. Secret access with redaction guarantees.
14. Resource budgets.
15. Declarative configuration and desired-state reconciliation.
16. Strong diagnostics for lifecycle and dependency problems.
17. A deliberately bounded record/replay facility.
18. Test utilities for plugin and agent authors.
19. A public API small enough to be learned quickly.
20. Explicit concurrency and lifecycle invariants.

------------------------------------------------------------------------

## 3. Non-goals

The project MUST NOT initially attempt to:

-   build another graph engine;
-   clone LangChain;
-   replace LangGraph checkpointing;
-   replace LangGraph Store;
-   build a universal LLM SDK;
-   invent an incompatible tool ecosystem;
-   replace LangSmith experiment management;
-   provide arbitrary Python code hot-module replacement as a core
    production feature;
-   claim deterministic replay of arbitrary external systems;
-   claim that in-process Python plugins are sandboxed;
-   use hidden global service locators;
-   require private LangGraph APIs;
-   rebuild all compiled graphs whenever any runtime capability changes;
-   implement distributed orchestration before the local runtime
    semantics are correct.

------------------------------------------------------------------------

## 4. Technology requirements

The reference implementation SHOULD use:

-   Python \>= 3.12
-   `asyncio`
-   `contextlib.AsyncExitStack`
-   `contextvars` where appropriate
-   Pydantic v2
-   `typing.Protocol`
-   immutable dataclasses where appropriate
-   `packaging.version.Version`
-   `packaging.specifiers.SpecifierSet`
-   LangGraph
-   `langchain-core`
-   LangSmith
-   pytest
-   pytest-asyncio
-   Ruff
-   Pyright

The project MUST use `pyproject.toml`.

The implementation MUST be async-first.

------------------------------------------------------------------------

## 5. Conceptual model

The harness owns composition and lifecycle:

``` text
Application
    |
    v
Harness
    |
    +-- Plugins
    +-- Capabilities
    +-- Scopes / Effects
    +-- Dependency Resolver
    +-- Runtime Generations
    +-- Hooks
    +-- Policy
    +-- Budgets
    +-- Secrets
    +-- Config / Reconciliation
    +-- Diagnostics
    |
    v
AgentRuntime
    |
    +-- LangGraphAgentRuntime
            |
            +-- StateGraph
            +-- Checkpointer
            +-- Store
            +-- Interrupts / Resume
            +-- Streaming
            +-- Subgraphs
            |
            v
        LangSmith
```

The harness answers:

-   Which components exist?
-   Which capabilities are available?
-   Which provider satisfies a capability?
-   Which plugins may activate?
-   What happens when a dependency disappears?
-   Who owns a resource?
-   How is an effect reverted?
-   Which runtime generation does a run observe?

LangGraph answers:

-   Which node executes next?
-   How does graph state change?
-   How is graph execution persisted?
-   How does execution pause and resume?
-   How are branches and subgraphs executed?
-   How is output streamed?

------------------------------------------------------------------------

## 6. Core domain types

### 6.1 CapabilityKey

A capability is a logical service contract.

Example:

``` python
MODEL = CapabilityKey("model", api_version="1")
TOOLS = CapabilityKey("tools", api_version="1")
MEMORY = CapabilityKey("memory", api_version="1")
POLICY = CapabilityKey("policy", api_version="1")
SECRETS = CapabilityKey("secrets", api_version="1")
```

A capability registration MUST include:

-   key;
-   API version;
-   provider plugin identity;
-   value/provider object;
-   metadata;
-   ownership information.

Python Protocols SHOULD be used for static contracts where useful.
Runtime Pydantic validation MUST NOT be forced on every service object.

### 6.2 PluginManifest

A manifest MUST include at least:

``` python
class PluginManifest(BaseModel):
    name: str
    version: str
    provides: dict[str, str] = Field(default_factory=dict)
    requires: dict[str, str] = Field(default_factory=dict)
    optional: dict[str, str] = Field(default_factory=dict)
    permissions: list[str] = Field(default_factory=list)
    config_version: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)
```

Version requirements MUST use `packaging` rather than a custom semver
implementation.

### 6.3 Plugin

The framework MUST support an ergonomic function-based plugin API and
MAY support a class-based API.

Example:

``` python
@plugin(
    name="web-search",
    version="1.2.0",
    requires={"tools": ">=1,<2", "http": ">=1,<2"},
)
async def web_search_plugin(ctx: PluginContext) -> None:
    ctx.tools.register(...)
```

A class-based form MAY be:

``` python
class SearchPlugin(Plugin):
    manifest = PluginManifest(...)

    async def setup(self, ctx: PluginContext) -> None:
        ...
```

Both MUST use the same lifecycle machinery.

### 6.4 Scope

Every mounted plugin instance MUST own a `Scope`.

A scope MUST own:

-   cleanup callbacks;
-   async context managers;
-   child tasks;
-   capability registrations;
-   tool registrations;
-   hook registrations;
-   child scopes;
-   other explicitly registered reversible effects.

The reference implementation SHOULD use `AsyncExitStack`.

### 6.5 RuntimeGeneration

A `RuntimeGeneration` is an immutable, coherent view of runtime
composition used by agent runs.

A generation MUST expose an immutable `CapabilitySnapshot`.

A run MUST acquire a generation before execution and MUST retain it for
its defined lifetime.

------------------------------------------------------------------------

## 7. Plugin lifecycle

The minimum lifecycle is:

``` text
PENDING
   |
   v
LOADING
   |
   v
ACTIVE
   |
   v
UNLOADING
   |
   v
DISPOSED

LOADING --> FAILED
```

Health is separate from lifecycle. An `ACTIVE` plugin MAY be `DEGRADED`.

### Requirements

-   A plugin with unsatisfied required capabilities MUST normally remain
    `PENDING`.
-   A plugin MUST activate when all required capabilities become
    satisfied.
-   A plugin MUST deactivate when a required capability ceases to be
    satisfied in the next runtime composition.
-   Setup failure MUST close the partially constructed scope.
-   No setup failure may leave owned registrations or resources behind.
-   Dependency cycles MUST be detected and reported.
-   Teardown MUST be dependency-safe.

------------------------------------------------------------------------

## 8. Reversible effects

Every effect created through harness APIs MUST have an owner.

Examples include:

-   capabilities;
-   tools;
-   hooks;
-   event subscriptions;
-   tasks;
-   HTTP clients;
-   DB pools;
-   temporary resources;
-   routes;
-   timers.

Registry operations SHOULD automatically register their inverse
operation with the current scope.

Example:

``` python
ctx.tools.register(tool)
```

MUST associate that registration with the plugin scope so explicit
`unregister_tool()` code is normally unnecessary.

Cleanup MUST be:

-   async-aware;
-   deterministic;
-   cancellation-aware;
-   resilient to individual cleanup failures;
-   observable.

Default cleanup SHOULD be reverse-order sequential cleanup.

Cleanup errors SHOULD be aggregated rather than stopping cleanup after
the first failure.

------------------------------------------------------------------------

## 9. Reactive dependency resolution

Given:

``` text
DatabasePlugin -> provides database
MemoryPlugin   -> requires database -> provides memory
AgentPlugin    -> requires memory
```

removing the database from desired state MUST cause the next resolved
composition to exclude/deactivate dependent components in
dependency-safe order.

Restoring the provider MUST allow eligible pending plugins to
reactivate.

The resolver MUST provide:

-   dependency graph construction;
-   capability version matching;
-   provider resolution;
-   cycle detection;
-   activation ordering;
-   teardown ordering;
-   diagnostic explanations.

YAML/configuration ordering MUST NOT define dependency semantics.

------------------------------------------------------------------------

## 10. Runtime generations and unload

Logical removal and physical disposal MUST be distinct.

If a plugin is removed while active runs still reference a generation
that can reach it:

1.  the plugin MUST be absent from newly published generations;
2.  existing runs MUST retain a coherent old generation;
3.  the old generation enters `DRAINING`;
4.  physical disposal MUST wait until no live generation/run can reach
    the plugin instance;
5.  only then may its scope be closed.

Core invariant:

> A plugin scope may be physically disposed only when no active runtime
> generation can reach it.

A generation SHOULD use leases or equivalent ownership accounting.

Unchanged plugin instances MAY be shared by multiple generations when
safe.

Reference/lease accounting MUST ensure a shared plugin is not disposed
until all generations referencing it have drained.

### Forced unload

A force operation MAY exist, but MUST be explicit and exceptional.

It MUST define whether dependent runs are cancelled, timed out, or
failed.

Normal unload MUST be graceful.

------------------------------------------------------------------------

## 11. Generation publication

Reconciliation SHOULD follow:

``` text
desired-state change
    |
    v
resolve next composition
    |
    v
construct/validate next generation
    |
    v
atomic publish
    |
    +--> new runs acquire new generation
    |
    +--> old generation drains
             |
             v
        leases reach zero
             |
             v
        unreachable scopes dispose
```

A partially constructed generation MUST never become visible to runs.

Control-plane mutation MAY use locks. The data path SHOULD NOT require a
global lock on every model/tool invocation.

------------------------------------------------------------------------

## 12. Build-time vs runtime-bound capabilities

The implementation MUST distinguish:

### Runtime-bound dependencies

Changing the concrete provider does not alter graph topology.

Examples:

-   model provider;
-   DB connection;
-   tenant context;
-   policy implementation;
-   secrets provider.

These SHOULD normally be supplied through the run context and MUST NOT
automatically trigger graph recompilation.

### Build-time dependencies

Changing the dependency changes compiled graph structure/configuration.

Examples may include:

-   state schema;
-   node topology;
-   conditional edges;
-   static ToolNode composition;
-   graph middleware topology.

Only relevant build-time changes SHOULD invalidate graph cache entries.

The implementation MUST NOT use "any capability changed =\> rebuild
every graph".

------------------------------------------------------------------------

## 13. LangGraph integration

LangGraph is the official first-class execution engine.

The implementation SHOULD use current public APIs such as, where
appropriate:

-   `StateGraph`;
-   `CompiledStateGraph`;
-   `Runtime`;
-   `Command`;
-   `Send`;
-   `interrupt`;
-   checkpointers;
-   Store;
-   streaming;
-   subgraphs.

Current upstream APIs MUST be inspected during implementation. This
specification MUST NOT be used to force obsolete API names.

The harness MUST NOT implement a competing graph engine.

------------------------------------------------------------------------

## 14. LangGraph runtime bridge

The primary bridge SHOULD use LangGraph's run-scoped runtime/context
facilities rather than globals.

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

Graphs SHOULD be compiled with an appropriate context schema.

Nodes SHOULD access the immutable run snapshot through the public
LangGraph runtime context.

------------------------------------------------------------------------

## 15. AgentRuntime abstraction

The core MUST depend on a small execution protocol, not directly on a
specific graph instance.

Example shape:

``` python
class AgentRuntime(Protocol):
    async def invoke(
        self,
        request: AgentRequest,
        run_context: HarnessRunContext,
    ) -> AgentResult:
        ...

    def stream(
        self,
        request: AgentRequest,
        run_context: HarnessRunContext,
    ) -> AsyncIterator[AgentEvent]:
        ...
```

The initial implementation is `LangGraphAgentRuntime`.

The abstraction MUST remain minimal and MUST NOT attempt to normalize
every possible execution backend prematurely.

------------------------------------------------------------------------

## 16. Models and tools interoperability

The harness SHOULD use existing `langchain-core` public interfaces where
sufficient.

It SHOULD work naturally with:

-   chat models;
-   `Runnable`;
-   `BaseTool`;
-   `StructuredTool`;
-   `@tool`.

Harness-specific metadata MAY wrap tools.

Example:

``` python
@dataclass(frozen=True)
class ToolPolicy:
    permissions: tuple[str, ...] = ()
    idempotent: bool = False
    side_effects: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    cost_class: str | None = None
```

The harness MUST NOT create a second incompatible tool execution
protocol without a demonstrated requirement.

------------------------------------------------------------------------

## 17. Tool registry and execution boundary

A registered tool SHOULD contain:

-   underlying LangChain-compatible tool;
-   owner plugin;
-   policy metadata;
-   arbitrary safe metadata.

Tool execution through the harness MUST be able to enforce:

-   permissions;
-   approval requirements;
-   budget checks;
-   timeout;
-   tracing;
-   error normalization.

Registration MUST be scope-owned and reversible.

------------------------------------------------------------------------

## 18. Hooks

The harness MAY support hook semantics such as:

-   observe;
-   transform;
-   bail;
-   around.

The initial hook surface SHOULD be intentionally small.

High-value boundaries include:

-   `plugin_mounting`;
-   `plugin_mounted`;
-   `plugin_unmounting`;
-   `plugin_unmounted`;
-   `before_agent_run`;
-   `after_agent_run`;
-   `agent_error`;
-   `before_tool_execute`;
-   `after_tool_execute`;
-   `tool_error`;
-   `generation_published`;
-   `generation_draining`;
-   `policy_decision`.

Hooks MUST:

-   support async handlers;
-   have deterministic ordering;
-   define exception semantics;
-   be scope-owned;
-   disappear automatically when the owner unloads.

The harness SHOULD prefer existing LangGraph/LangChain mechanisms for
graph-internal callbacks where they already solve the problem.

------------------------------------------------------------------------

## 19. Structured concurrency

Plugins MUST NOT be encouraged to create unowned immortal tasks.

The framework SHOULD expose:

``` python
ctx.tasks.create_task(...)
```

or an equivalent scope-owned API.

`asyncio.TaskGroup` SHOULD be used where appropriate.

Scope shutdown MUST define:

1.  stop accepting new work;
2.  request cancellation of owned child tasks;
3.  await task completion according to shutdown policy;
4.  execute remaining disposers.

------------------------------------------------------------------------

## 20. Policy

Capability availability and authorization are distinct.

Possible permissions include:

``` text
filesystem.read
filesystem.write
filesystem.delete
network.fetch
shell.execute
database.query
secrets.read
```

Future scope-aware forms MAY include:

``` text
filesystem.read:/workspace/**
filesystem.write:/workspace/output/**
```

A tool invocation SHOULD flow through:

``` text
agent -> tool request -> harness executor -> policy -> optional approval -> tool
```

Security invariant:

> In-process Python plugins are trusted code.

Policy protects harness-controlled operations. It is not a sandbox
against malicious plugin code.

------------------------------------------------------------------------

## 21. Secrets

Plugins SHOULD access secrets through a `SecretProvider`.

Example:

``` python
value = await ctx.secrets.get("openai.api_key")
```

The first implementation MAY use environment variables.

Future providers MAY include Vault, AWS Secrets Manager, GCP Secret
Manager, and 1Password.

Secret values MUST NOT appear in:

-   traces;
-   runtime snapshots;
-   diagnostics;
-   replay records;
-   exception messages.

Redaction MUST be tested.

------------------------------------------------------------------------

## 22. Budgets

The architecture SHOULD support:

-   wall-clock deadlines;
-   model call count;
-   tool call count;
-   tokens;
-   estimated monetary cost;
-   child-agent runs.

Not every budget dimension is required for v0.1.

Budgets SHOULD support inheritance/allocation to child runs.

Enforcement MUST occur only at boundaries the harness actually controls.

------------------------------------------------------------------------

## 23. Persistence boundaries

LangGraph checkpointers own durable graph/thread execution state.

LangGraph Store SHOULD be used where appropriate for
cross-thread/long-term agent data.

The harness owns separate data such as:

-   plugin configuration;
-   runtime generation metadata;
-   runtime snapshots;
-   plugin manifests;
-   replay records;
-   deployment metadata.

These concerns MUST NOT be collapsed into a second graph checkpoint
system.

------------------------------------------------------------------------

## 24. LangSmith and telemetry

Native LangGraph/LangChain LangSmith tracing SHOULD be used for model,
tool, and graph execution.

The harness SHOULD add explicit instrumentation for operations not
naturally visible to LangGraph, including:

-   plugin mount/unmount;
-   dependency resolution;
-   reconciliation;
-   generation publication/draining;
-   policy decisions;
-   budget exhaustion;
-   graph build/cache events.

Useful metadata includes:

-   harness version;
-   agent version;
-   generation ID;
-   plugin graph hash;
-   plugin versions;
-   capability versions;
-   graph definition hash;
-   environment.

Metadata MUST be filtered and redacted.

OpenTelemetry MAY be supported as an interoperability layer. The harness
SHOULD NOT create a competing telemetry universe.

------------------------------------------------------------------------

## 25. Runtime snapshots

Every run SHOULD be attributable to an immutable runtime snapshot
containing enough information to explain the composition that produced
it.

Example:

``` json
{
  "harness_version": "0.1.0",
  "generation_id": "gen_018",
  "agent_runtime": "langgraph",
  "plugins": {
    "openai-model": "1.4.0",
    "web-tools": "2.1.0"
  },
  "capabilities": {
    "model": "1",
    "tools": "1"
  },
  "config_hash": "...",
  "plugin_graph_hash": "...",
  "graph_definition_hash": "...",
  "tool_schema_hash": "...",
  "prompt_hash": "..."
}
```

Hashes MUST be based on documented canonical serialization.

Snapshots MUST NOT contain secrets.

------------------------------------------------------------------------

## 26. Graph caching

Compiled graph cache keys MUST depend only on relevant build-time
inputs.

They SHOULD NOT depend on the entire mutable runtime.

Changing a runtime-bound model provider SHOULD normally preserve graph
cache reuse.

------------------------------------------------------------------------

## 27. Configuration and reconciliation

The project SHOULD support declarative configuration.

Example:

``` yaml
plugins:
  - id: model
    plugin: openai-model
    config:
      model: example-model

  - id: search
    plugin: web-search

  - id: agent
    plugin: langgraph-agent
```

Entries MUST have stable identity.

The reconciler compares desired state with actual state and derives
operations such as:

-   mount;
-   unmount;
-   reconfigure;
-   replace.

The initial reconciler SHOULD be local and deterministic.

It MUST NOT attempt Kubernetes-scale distributed control-plane semantics
in v0.1.

------------------------------------------------------------------------

## 28. Diagnostics

The public diagnostics surface SHOULD include equivalents of:

``` python
runtime.plugins()
runtime.capabilities()
runtime.dependencies()
runtime.generations()
runtime.status()
```

For a plugin, diagnostics SHOULD explain:

-   lifecycle state;
-   health;
-   why it is pending;
-   missing or incompatible dependencies;
-   provider selected for each dependency;
-   capabilities provided;
-   owning scope;
-   generation references;
-   owned effects/resources where safe to expose.

------------------------------------------------------------------------

## 29. Error model

The public API SHOULD use typed errors such as:

-   `PluginLoadError`
-   `PluginSetupError`
-   `PluginDependencyError`
-   `PluginCycleError`
-   `CapabilityNotFound`
-   `CapabilityVersionMismatch`
-   `ScopeClosedError`
-   `EffectCleanupError`
-   `HookExecutionError`
-   `PolicyDenied`
-   `BudgetExceeded`
-   `ToolExecutionError`
-   `GenerationConflictError`
-   `ReplayMismatch`

Errors SHOULD carry structured diagnostics where appropriate.

------------------------------------------------------------------------

## 30. Replay

Replay is intentionally bounded.

Initial record mode SHOULD be able to capture selected boundaries such
as:

-   runtime snapshot;
-   model request/response;
-   tool request/response;
-   interrupt values;
-   important lifecycle events.

Modes MAY be:

``` text
live
record
replay
```

Replay MUST explicitly report unsupported/unrecorded operations.

The project MUST NOT claim deterministic replay of arbitrary clocks,
randomness, HTTP, external databases, or services unless those
boundaries are explicitly virtualized.

------------------------------------------------------------------------

## 31. Evaluation

LangSmith SHOULD remain the primary experiment/evaluation integration.

The harness SHOULD make an agent invocation easy to expose as a
LangSmith evaluation target.

Evaluation metadata SHOULD identify:

-   generation;
-   model configuration;
-   tool set;
-   prompt version;
-   plugin graph.

Harness-specific evaluator plugins MAY exist, but SHOULD NOT recreate a
competing experiment platform.

------------------------------------------------------------------------

## 32. Multi-tenancy

The architecture MUST NOT assume a single user or tenant.

Run context SHOULD support:

-   tenant identity;
-   user identity;
-   request metadata.

Capability providers MAY eventually be:

-   global;
-   tenant-scoped;
-   run-scoped.

Complex multi-tenant orchestration is not required for v0.1, but scope
boundaries MUST not prevent it.

------------------------------------------------------------------------

## 33. Public API expectations

Normal application code SHOULD remain small.

Example:

``` python
async with Harness(config) as app:
    result = await app.agents.invoke(
        "research-agent",
        {"messages": [...]},
        user_id="user-123",
    )
```

Programmatic installation SHOULD be possible:

``` python
app = Harness()

app.install(OpenAIPlugin(...))
app.install(WebSearchPlugin(...))
app.install(MyAgentPlugin(...))

await app.start()
```

Normal users SHOULD NOT need direct access to internal resolver/registry
implementation objects.

------------------------------------------------------------------------

## 34. Shutdown semantics

Graceful harness shutdown MUST:

1.  stop accepting new runs;
2.  mark current generations as draining;
3.  wait for active runs according to configured timeout;
4.  optionally cancel remaining runs after timeout;
5.  dispose unreachable plugin scopes;
6.  close infrastructure resources;
7.  aggregate/report cleanup failures.

Shutdown MUST be idempotent.

------------------------------------------------------------------------

## 35. Required examples

The repository MUST eventually include:

### Example A --- Basic LangGraph agent

Demonstrate:

-   model capability;
-   tool registry;
-   LangGraph runtime context;
-   checkpointer;
-   streaming;
-   LangSmith tracing;
-   interrupt/resume;
-   runtime snapshot metadata.

### Example B --- Reactive dependency cascade

Demonstrate:

``` text
database -> memory -> agent extension
```

Remove database, verify dependent components become unavailable in the
next composition, then restore it and verify reactivation.

### Example C --- Safe provider replacement

Demonstrate:

``` text
Generation 1: Provider A
active run starts
provider changes
Generation 2: Provider B
old run completes on Generation 1
new run uses Generation 2
Generation 1 drains and disposes
```

------------------------------------------------------------------------

## 36. Testing requirements

The project MUST provide deterministic tests for at least:

-   plugin activation;
-   plugin pending state;
-   plugin setup failure;
-   capability registration/removal/replacement;
-   version mismatch;
-   dependency cascades;
-   cycle detection;
-   scope cleanup;
-   cleanup after setup failure;
-   cleanup error aggregation;
-   hook ownership and ordering;
-   task cancellation;
-   generation publication;
-   generation leases;
-   generation draining;
-   active run during provider replacement;
-   shared plugin lifetime across generations;
-   LangGraph run-context propagation;
-   checkpoint/resume;
-   interrupt/resume;
-   streaming;
-   LangSmith metadata;
-   policy denial;
-   budget exhaustion;
-   stable snapshot hashing;
-   secret redaction;
-   supported record/replay boundaries.

Concurrency tests MUST attempt to expose race conditions around
publication, acquisition, draining, and disposal.

------------------------------------------------------------------------

## 37. Test utilities

Provide a `TestHarness` and useful fakes, such as:

-   fake chat model;
-   fake tool;
-   fake secrets;
-   fake policy;
-   fake telemetry;
-   fake checkpointer.

Example:

``` python
async with TestHarness() as app:
    app.provide(MODEL, FakeChatModel(...))
    app.install(MyPlugin())
    ...
```

------------------------------------------------------------------------

## 38. Milestones

### Milestone 1 --- Lifecycle kernel

Implement only:

-   Scope;
-   AsyncExitStack integration;
-   Plugin;
-   PluginManifest;
-   CapabilityKey;
-   CapabilityRegistry;
-   DependencyResolver;
-   PENDING/LOADING/ACTIVE/UNLOADING/DISPOSED/FAILED;
-   mount/unmount;
-   dependency cascade;
-   cycle detection.

Do not integrate LangGraph yet.

Exit criteria: lifecycle invariants are exhaustively tested.

### Milestone 2 --- Generations and concurrency

Implement:

-   RuntimeGeneration;
-   CapabilitySnapshot;
-   generation publication;
-   leases/reference accounting;
-   draining;
-   safe provider replacement;
-   structured task ownership.

Exit criteria: active-run consistency and physical-disposal invariants
are proven by tests.

### Milestone 3 --- Tools, policy, hooks, secrets, budgets

Implement:

-   LangChain-compatible ToolRegistry;
-   tool metadata;
-   minimal hook registry;
-   policy boundary;
-   SecretProvider;
-   basic budget governor.

### Milestone 4 --- LangGraph integration

Implement:

-   HarnessRunContext;
-   AgentRuntime protocol;
-   LangGraphAgentRuntime;
-   graph cache;
-   build-time/runtime-bound dependency distinction;
-   checkpointer support;
-   Store support where appropriate;
-   streaming;
-   interrupt/resume.

### Milestone 5 --- LangSmith and snapshots

Implement:

-   native LangSmith tracing;
-   harness lifecycle instrumentation;
-   generation metadata;
-   runtime snapshots;
-   canonical hashing;
-   redaction;
-   optional OpenTelemetry bridge.

### Milestone 6 --- Configuration and reconciliation

Implement:

-   Pydantic plugin configs;
-   YAML loading;
-   stable plugin entry IDs;
-   desired-state diff;
-   mount/unmount/reconfigure/replace;
-   diagnostics.

Arbitrary Python HMR remains out of scope.

### Milestone 7 --- Replay and evaluation

Implement a bounded first version of:

-   model/tool boundary recording;
-   supported replay;
-   LangSmith dataset/experiment integration;
-   evaluation metadata.

------------------------------------------------------------------------

## 39. Development process

Before implementation:

1.  inspect the repository;
2.  inspect current LangGraph, langchain-core, LangSmith, Cordis, and
    DeepSeek Harness APIs/docs;
3.  record selected dependency versions;
4.  document upstream assumptions;
5.  implement the lifecycle kernel before LangGraph integration.

For every milestone:

-   implement tests alongside production code;
-   run relevant tests;
-   run Ruff;
-   run Pyright;
-   fix failures before proceeding;
-   update architecture documentation;
-   avoid placeholders presented as complete functionality.

If current upstream APIs conflict with examples in this specification,
prefer current documented public APIs and document the deviation.

------------------------------------------------------------------------

## 40. Acceptance criteria

The project is considered architecturally successful when all of the
following are true:

1.  A plugin can register a capability/tool/hook/task without manually
    writing corresponding deregistration logic.
2.  Removing a required capability produces deterministic dependency
    cascades.
3.  Failed plugin setup leaks no owned effects.
4.  An active run never observes a partially reconciled runtime.
5.  Provider replacement does not mutate the runtime underneath an
    active run.
6.  Old generations drain before physically disposing resources they
    still reach.
7.  Shared plugin instances are not disposed while referenced by another
    live generation.
8.  Runtime-bound provider changes do not cause unnecessary LangGraph
    recompilation.
9.  LangGraph checkpointing and Store are not duplicated by the harness.
10. LangChain-compatible tools/models work without unnecessary adapter
    layers.
11. LangSmith traces can be associated with the exact runtime
    generation/snapshot.
12. Secrets are absent from snapshots, traces, diagnostics, replay
    records, and exception strings.
13. In-process plugin trust assumptions are explicitly documented.
14. Shutdown is graceful, deterministic, and idempotent.
15. Diagnostics can explain why a plugin is inactive.
16. The core lifecycle and generation semantics are covered by
    concurrency tests.

------------------------------------------------------------------------

## 41. Definition of success

A plugin author should be able to write approximately:

``` python
class SearchPlugin(Plugin):
    manifest = PluginManifest(
        name="search",
        version="1.0.0",
        requires={"tools": ">=1,<2"},
    )

    async def setup(self, ctx: PluginContext) -> None:
        ctx.tools.register(
            search_tool,
            policy=ToolPolicy(
                permissions=("network.fetch",),
                idempotent=True,
            ),
        )
```

without manually managing:

-   dependency activation;
-   cleanup;
-   tool unregistration;
-   task ownership;
-   runtime-generation consistency;
-   tracing correlation;
-   graph-cache invalidation.

The intended result is:

> Cordis-inspired reactive composition + Python-native scoped resource
> management + immutable runtime generations + native LangGraph
> execution + LangSmith observability, with strict separation of
> responsibilities.

