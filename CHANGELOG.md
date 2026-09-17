# Changelog

All notable changes to Chassis are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) with the pre-1.0 caveat
that a minor release may break the documented surface.

## [0.2.0] - 2026-09-17

Makes the kernel smaller, clearer, and harder to misuse. No new agent-framework
abstraction: composition, ownership, generation publication, and disposal behave as
they did in 0.1.

### Added

- **Optional integrations.** The core installs without `langgraph`,
  `langchain-core`, or `langsmith`. Extras: `chassis-harness[langgraph]`,
  `chassis-harness[langsmith]`. A missing extra raises `MissingExtraError` (an
  `ImportError`) naming the extra to install. The LangSmith and replayable-model
  modules, and the langchain-core test doubles, load lazily.
- **Generation pressure diagnostics.** `harness.diagnostics.generation_pressure()`
  returns a structured `GenerationPressureReport`: the current generation, live and
  draining counts, per-generation age and lease count, the oldest outstanding lease
  age, the plugin instances an old generation still retains, and which live
  generations reach a given instance. It also renders `to_text()` and vendor-neutral
  `metrics()` gauges. `harness.diagnostics.instance_generations(instance_id)`
  answers "who still reaches this instance?".
- **Lease identity and age.** `GenerationAccounting` records each lease's start
  time, so `GenerationManager.acquire_lease()`/`release_lease()` and
  `RuntimeGeneration.oldest_lease_age_seconds` expose authoritative lease age.
  `GenerationManager` gained an injectable clock, `history_limit`, and an
  `evicted` counter.
- **Explicit budget enforcement semantics.** `BudgetEnforcement`
  (`enforced`/`accounted`), `BudgetDimension.enforcement`, `BudgetLimit`, and
  `BudgetLimits.describe()`/`enforced_dimensions()`/`accounted_dimensions()` make it
  impossible to mistake a token or cost limit for a guarantee Chassis does not make.
  `BudgetGovernor.record(model_calls=…, tokens=…, estimated_cost=…)` is the canonical
  way an integration reports usage Chassis cannot observe. `harness.diagnostics.budgets()`
  reports the configured defaults with their enforcement modes.
- **`chassis.tools.Tool`.** The tool boundary is now a structural protocol (`name`,
  `description`, `ainvoke`), so the core does not import a tool library. A
  `langchain-core` `BaseTool` satisfies it unchanged.

### Changed

- `chassis.ToolSnapshot.to_langchain_tools()` is now `to_tools()`, since the core no
  longer names a specific tool library.
- `GenerationManager.acquire()`/`release()` are now
  `acquire_lease()`/`release_lease()`, returning an identity-bearing
  `GenerationLease`. `Harness.acquire()` still yields the `RuntimeGeneration`.
- `RuntimeSnapshot` tool hashing tolerates a tool with no `get_input_schema`,
  recording `args: null` instead of failing.
- `chassis.telemetry`, `chassis.replay`, and `chassis.testing` import their
  langchain/langsmith-dependent members lazily; `chassis.telemetry.langsmith` is not
  imported unless a LangSmith object is used.

### Known limitations

- In-process Python plugins are trusted code; the policy engine is not a sandbox.
- Replay does not virtualize clocks, randomness, networks, databases, or the
  filesystem, and only tool and model boundaries are replayable.
- Configuration changes are reconciled as replacements, not in-place
  reconfiguration.
- An abandoned `AgentRegistry.stream` generator holds its generation lease until it
  is closed or collected.
- Accounted budget dimensions (`model_calls`, `tokens`, `estimated_cost`) are only
  enforced when the integration that owns the model call reports usage; Chassis
  cannot observe them itself.
- Generation pressure reports; it does not evict, cap, or refuse. A loitering
  generation is visible, not reclaimed.

## [0.1.0] - 2026-09-16

First release. The distribution is `chassis-harness`; the import package is
`chassis`; Python 3.12+.

### Added

- Scoped ownership and reversible effects: every harness-managed effect belongs to
  exactly one scope, and a failed plugin setup reverts everything that setup created.
- Versioned capability contracts, provider registry, and immutable capability
  snapshots.
- Plugin manifests and author API: `provides`, `requires`, `optional`, permissions,
  and a resource-oriented context (`ctx.tools`, `ctx.hooks`, `ctx.agents`,
  `ctx.tasks`, `ctx.secrets`, `ctx.cleanup`).
- Reactive dependency resolution with deterministic ordering, pending diagnosis,
  and cycle detection.
- Immutable runtime generations with atomic publication, leases, draining, and
  reachability-gated physical disposal (logical unload is not destruction).
- Scope-owned tool registry and the execution boundary: policy, approval, budget,
  deadline, hooks, tracing, error normalization, and replay.
- Scope-owned hook registry with deterministic ordering over every declared
  boundary: tool execution, agent runs, plugin lifecycle, generation publication
  and draining, and policy decisions.
- Hierarchical budget governor: wall clock, tool calls, and child runs enforced at
  harness boundaries; model calls, tokens, and estimated cost declared and reported,
  with an explicit escape hatch for code that owns the model call.
- Secret providers with mandatory redaction across logs, traces, snapshots,
  diagnostics, replay records, and error strings.
- LangGraph integration behind a minimal `AgentRuntime` protocol: agent definitions,
  build-time-keyed graph caching, checkpointing, `Store`, streaming, and
  interrupt/resume, with the run's immutable snapshot propagated through LangGraph's
  public runtime context.
- LangSmith telemetry and runtime snapshots: lifecycle spans and events, generation
  metadata, canonical hashing, and redaction.
- Declarative configuration: YAML or JSON desired state, a plugin catalog, drift
  reporting, and reconciliation through the same path as programmatic composition.
  A configuration can be passed to `Harness(...)` and is applied on start.
- Bounded record/replay of tool and model boundaries, with snapshot, lifecycle, and
  interrupt boundaries recorded as attribution.
- Diagnostics that explain state from authoritative data: plugins, capabilities,
  dependencies, generations, tools, hooks, agents, owned effects, desired state, and
  `explain()` for why a plugin is (in)active.
- `TestHarness` and deterministic fakes, plus four self-asserting examples and
  concurrency tests for generation lifetime and shutdown races.

### Known limitations

- In-process Python plugins are trusted code; the policy engine is not a sandbox.
- Replay does not virtualize clocks, randomness, networks, databases, or the
  filesystem, and only tool and model boundaries are replayable.
- Configuration changes are reconciled as replacements, not in-place
  reconfiguration.
- An abandoned `AgentRegistry.stream` generator holds its generation lease until it
  is closed or collected.
