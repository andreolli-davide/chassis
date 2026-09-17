# Changelog

All notable changes to Chassis are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) with the pre-1.0 caveat
that a minor release may break the documented surface.

## [0.5.0] - 2026-09-17

Introduces **first-class, versioned agent composition** on top of the existing
kernel without turning Chassis into an agent framework. `AgentSpec` describes what
runtime composition a logical agent sees, materializes into the existing composition
scopes, and is published as an immutable revision. It does not execute, plan,
remember, or authorize; LangGraph's `AgentDefinition` remains the engine-specific
graph-build contract. Every 0.4 guarantee still holds.

### Added

- **`AgentSpec` and `AgentRevision`.** `chassis.agents` (backed by
  `chassis.agent_spec`) exposes an immutable, engine-neutral description of an
  agent's composition — name, author-declared revision, scope path, runtime
  reference, capability view, requirements, plugin contributions, tool view,
  profile, and metadata. Every container is frozen recursively, so an authoring
  object cannot alias mutable state into a published revision. `AgentRevision`
  records a published revision and its materialization facts.
- **Agent revision registry and materialization.** `AgentRegistry` gained
  `install`/`replace`/`remove`/`spec`/`active_spec`/`revisions`/`specs`/`history`.
  A spec materializes into a real `CompositionScope` (default `/agents/<name>`)
  that owns its plugin contributions, capability and tool views, requirements, and
  reserved metadata naming the agent and revision. A published `(name, revision)`
  is immutable; changing the active revision requires `replace`; retirement
  withdraws the active revision from desired state without touching published
  generations, and historical revisions stay reachable.
- **Run pinning and attribution.** `HarnessRunContext`, `AgentResult`, and
  `AgentEvent` carry `agent_revision`; invocation reads the revision the acquired
  generation actually materialized, so a run stays associated with the revision it
  started under even when a newer revision is published concurrently.
  `RuntimeSnapshot` gained `agent_revision`, `agent_identity`, and an additive
  `public_composition_digest()` alias for `semantic_digest()`.
- **Tool visibility in composition scopes.** Tools follow the same
  inheritance-plus-narrowing rule as capabilities: a scope declares a tool view,
  `build_scope_tree` binds it to the tools the mounted instances own, and
  `Harness.tool_snapshot(..., scope=)`/`run_environment(..., scope=)` narrow a run's
  tools to its scope. Sibling scopes no longer leak tools into each other.
- **Agent diagnostics.** `harness.diagnostics.explain_agent(...)` reports a
  revision's scope, views, contributions, requirement provenance, visible providers
  and tools, composition digest, and the generations that published it;
  `harness.diagnostics.diff_agents(...)` compares two revisions structurally and
  delegates the runtime reuse/rebuild analysis to the existing generation impact
  engine (`AgentExplanation`, `AgentDiff`).
- **Optional `PluginManifest.implementation_revision`.** An author-declared identity
  for two builds that share `name@version` and a module qualname; it participates in
  the implementation fingerprint, and an absent value keeps the previous identity.
- **Guarantees G21–G24** and a new [agent-composition.md](docs/agent-composition.md)
  guide, plus `examples/agent_composition.py`.

### Changed

- `RuntimeSnapshot.to_dict()` (and therefore `digest()`) gained `agent_revision`;
  stored 0.4 snapshots should be re-baselined.
- `harness.agents.invoke`/`stream` stamp the logical agent name and revision on the
  result and streamed events. An agent registered only as a runtime is unchanged.
- `build_scope_tree` accepts the live tool registrations and records tool visibility
  in the published scope tree, so scope topology and tool views both participate in
  the generation digest as composition.

## [0.4.1] - 2026-09-17

Hardening found by an architectural review of 0.4's incremental reuse model. No
public API changes.

### Fixed

- **A per-entry `provider_preference` no longer makes desired-state reconciliation
  non-convergent.** The desired fingerprint was computed with the preference while
  the installed fingerprint was not, so re-applying the same declarative
  configuration derived `REPLACE` on every apply and bumped the revision each time.
  Preferences disambiguate resolution rather than configure the plugin; a change
  that selects a different provider is reported through the consumer's dependency
  bindings as `REWIRED`.
- **A published generation no longer shares a mutable configuration mapping with
  the control plane.** `PluginEntry.config` is frozen at install time, so mutating a
  desired entry can no longer change the configuration an already-running
  generation observes, or the `config_hash` of its snapshot.
- **An unchanged composition no longer republishes a generation.** Scope
  provenance records the instance actually mounted rather than the resolver's
  pre-mount cached id, so full scope-tree equality does not churn on a physical id
  that is not part of composition identity.
- **Plugin object reprs no longer render configuration.** `PluginInstance` and
  `PluginEntry` exclude the effective configuration (and `PluginInstance` its
  error) from their dataclass repr, which previously reached logs and assertion
  output.

## [0.4.0] - 2026-09-17

Makes composition **incremental**: pending and published composition now carry a
semantic identity, so a change rebuilds only the nodes it actually affects and
everything else is the exact runtime instance it already was. No agent-framework
abstraction is introduced, and Chassis still never executes an agent. Every 0.3
guarantee still holds, and a composition with no changes reconciles exactly as
before.

### Added

- **Semantic identity.** `chassis.core.identity` (re-exported from
  `chassis.composition`) introduces `SemanticIdentity` and `DependencyBinding`.
  Identity covers the inputs that can affect observable behaviour — implementation,
  capability contracts, effective configuration, scope path, and resolved dependency
  bindings — and keeps separate fingerprints for implementation, contracts,
  configuration, and dependencies, so a rebuild can be explained as
  *"config changed"* rather than *"hash mismatch"*. `PluginInstance.semantic_identity`
  records the identity a node was mounted with; `RuntimeGeneration.identities` and
  `identity_for(entry_id)` expose it as a view of the published composition.
- **Incremental impact analysis.** `ImpactAnalysis` and `NodeImpact` report
  `UNCHANGED`/`REUSED`/`REBUILT`/`REWIRED`/`ADDED`/`REMOVED` with a small, explicit
  reason vocabulary (`config_changed`, `implementation_changed`,
  `dependency_changed`, `scope_visibility_changed`, `provider_selection_changed`,
  `capability_contract_changed`, `preference_changed`). `ReconcileResult.impact`
  carries the analysis for the reconciliation that just ran; it follows real
  dependency bindings, not scope membership.
- **Safe structural sharing.** A node is carried into the new generation only when
  its recomputed semantic identity equals the one it was mounted with, so multiple
  live generations reference the same runtime instance without any generation
  observing a mutation. A consumer whose selected provider changed is rebuilt
  (reported as `REWIRED`) instead of being reused with a stale registration.
- **Reuse diagnostics.** `harness.diagnostics.analyze_impact(old, new)` returns the
  full analysis; `harness.diagnostics.explain_reuse(old, new, node)` returns a
  structured `ReuseExplanation` (decision, reasons, changed inputs, dependency
  changes, shared instance id). `GenerationDiff` gained a semantic `NODES` section
  and `by_decision(...)`.
- **Shared reachability in generation pressure.**
  `GenerationPressureReport.resources` (`ResourceReachability`) lists, per live
  resource, the generations that reach it and why it is retained (`lease` when a run
  holds a reaching generation, `sharing` when more than one live generation reaches
  it). Adds the `chassis.resources.shared` gauge.
- **Snapshot semantic identity.** `RuntimeSnapshot.semantic_digest()` hashes only the
  semantic composition; `physical_digest()` hashes the runtime instance ids;
  `runtime_instance_ids` and `semantic_scopes` are new fields.

### Changed

- Reconciliation reuses by semantic identity rather than by entry revision alone.
  A consumer whose selected provider changed is rebuilt and re-resolves; unrelated
  nodes are reused. See [migration.md](docs/migration.md#03-04).
- `PluginRegistry.mount(..., supersede=True)` lets a rebuild mount a fresh instance
  for an unchanged revision without disposing a still-reachable predecessor.
- `RuntimeSnapshot.to_dict()` (and therefore `digest()`) gained
  `runtime_instance_ids`; stored 0.3 snapshots should be re-baselined.
- `harness.diagnostics.diff_generations` now reports a semantic NODES section in
  addition to the structural scopes/providers/requirements changes.

### Guarantees

- **G18 — safe semantic reuse.** A runtime node is shared across generations only
  when its semantic identity proves reuse cannot change observable behaviour.
- **G19 — reachability lifetime.** A shared runtime resource is not disposed while
  any live generation can reach it.
- **G20 — explainable incremental publication.** Unaffected, semantically identical
  nodes are eligible for reuse, and Chassis reports reuse only for a node whose
  reuse safety it established.

### Design decisions

- Identity is a conservative proof, not a heuristic: anything Chassis cannot prove
  safe is rebuilt. A changed revision rebuilds even when the new node is
  semantically identical.
- The configuration fingerprint is computed from the effective configuration so a
  credential change forces a rebuild, and is never emitted; `semantic_digest()`
  therefore stays redacted and a secret-only change is invisible in it.
- Sharing is never shared mutability: a reused instance is never reconfigured in
  place, so a published generation still never observes a composition change after
  publication.
- Disposal keeps following reachability through the existing lease machinery;
  incremental reuse added no second lifetime system.

### Known limitations

- Reuse is per process and follows generation reachability; there is no
  content-addressed build cache, no cross-process sharing, and no persistent
  composition graph.
- Impact is conservative. A scope narrows the search for affected nodes but never
  replaces dependency analysis.
- The semantic digest reflects redacted configuration, so a secret-only change is
  invisible in it while still forcing a rebuild.
- Unchanged from 0.3: in-process Python plugins are trusted code; replay does not
  virtualize clocks, randomness, networks, databases, or the filesystem.

## [0.3.0] - 2026-09-17

Adds **hierarchical scoped composition** and **explainable composition provenance**.
No agent-framework abstraction is introduced: a scope is a generic composition
primitive, not an agent, tenant, or session, and Chassis still never executes an
agent. Every 0.2 guarantee still holds, and a composition with no declared scopes
behaves exactly as it did.

### Added

- **Composition scopes.** `chassis.composition` introduces `CompositionScope` and
  `CompositionTree` (desired state, reached through `harness.composition`) and the
  immutable `ScopeTree` of `ResolvedScope` (published state, reached through
  `RuntimeGeneration.scopes`). A scope inherits the providers visible from its
  ancestors, adds local providers, declares its own requirements, narrows the
  composition it exposes with a capability view, nests, and owns the entries it
  declares.
- **`Harness.install(..., scope=...)`** and `CompositionScope.install(...)`,
  `.require(...)`, `.restrict(...)`, `.unrestrict()`, `.child(...)`,
  `.set_metadata(...)`.
- **Scope-aware resolution with provenance.** The resolver accepts a scope hierarchy
  and records, for every requirement, each candidate provider, its origin scope,
  whether it was visible and eligible, its rejection reason, the selected provider,
  and the selection reason. New structured types: `ScopePlan`,
  `ProviderAssessment`, and provenance fields on `RequirementResolution`
  (`consumer`, `provider_scope`, `provider_origin`, `selection_reason`,
  `assessments`). New statuses: `not_visible`, `provider_pending`.
- **Scoped provider preference.** `prefer_provider(capability, provider, *,
  consumer=..., scope=...)` disambiguates most-specific-first: consumer, then scope,
  then global.
- **Explain and diff diagnostics.**
  `harness.diagnostics.explain_requirement(consumer, capability, *, scope=..., generation_id=...)`
  returns structured `RequirementExplanation`;
  `harness.diagnostics.explain_scope(path, *, generation_id=...)` returns
  `ScopeExplanation` (parent, children, capability view, local/inherited/visible
  providers, requirements, unresolved requirements, owned registrations, tools,
  hooks, redacted metadata); `harness.diagnostics.diff_generations(old, new, *,
  include_unchanged=False)` returns a semantic `GenerationDiff` over
  `CompositionChange` records (`ADDED`/`REMOVED`/`REPLACED`/`REWIRED`/`CHANGED`/
  `UNCHANGED`). `harness.diagnostics.scopes()` lists the resolved tree.
- **Scoped snapshots.** `RuntimeSnapshot.scopes` identifies the scope tree, each
  scope's local providers, its capability view, and every requirement selection,
  using identities only — never configuration or scope metadata values.
- `examples/scoped_composition.py`, a runnable end-to-end example.

### Changed

- **`RuntimeSnapshot.digest()` now covers scope structure.** Topology and
  resolution are observable through the generation a run acquires, so a change there
  changes the digest. Snapshots recorded with 0.2 should be re-baselined; see
  [migration.md](docs/migration.md#02-03).
- Scope topology is part of composition identity: adding a scope, narrowing a view,
  or changing a selected provider publishes a new generation even when no instance
  changed.
- `Diagnostics.plugins()` reports each entry's `scope`;
  `Diagnostics.dependencies()` and `Diagnostics.status()` report the resolved scope
  tree and its size.
- `Harness.plan()` resolves with the declared scope hierarchy, so dry-run
  diagnostics explain scoped resolutions before publication.

### Guarantees

- **G15 — scoped visibility.** A scope observes its own local composition, the
  composition inherited from its ancestors, and what its capability view permits;
  sibling-local composition is not implicitly visible.
- **G16 — published scope immutability.** The scope tree and every
  composition-affecting field of a published generation are immutable, and
  scope-affecting changes become visible only through a new generation.
- **G17 — explainability.** Every resolved requirement has an authoritative
  provenance record (selected provider, origin scope, selection reason) and every
  unresolved requirement has an authoritative reason.

### Design decisions

- Ambiguity stays explicit rather than gaining a shadowing rule: a valid local
  provider and a valid inherited provider are both candidates until a preference
  selects one.
- Capability narrowing is composition visibility, not authorization; in-process
  plugins remain trusted code.
- A composition scope owns its entries logically; the physical owner of a
  registration remains the plugin instance's `Scope`, so rollback, reachability, and
  disposal reuse the existing lifecycle instead of adding a second teardown system.

### Known limitations

- Provider interception (`provider → wrapper → transformed provider`) is not part of
  0.3.
- Composition scopes are declarative-programmatic; the YAML configuration schema
  still describes a flat composition.
- Unchanged from 0.2: in-process Python plugins are trusted code; replay does not
  virtualize clocks, randomness, networks, databases, or the filesystem.

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
