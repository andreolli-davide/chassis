# Changelog

All notable changes to Chassis are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) with the pre-1.0 caveat
that a minor release may break the documented surface.

## [Unreleased]

### Fixed

- **Persistence canonicalization hardened** (roadmap R021). Canonical hashing
  now requires string mapping keys — mixed or non-string keys raise
  `ConfigurationError` instead of being stringified into collisions — and the
  primitive treatments are documented and pinned by deterministic tests: Unicode
  is hashed without silent normalization, signed zero and integral floats
  normalize together, non-finite floats are deterministic, `Decimal`-like values
  are rejected rather than guessed, and nested key order never affects a digest.
  The 12-digit float rounding is now documented (values that differ only beyond
  the 12th decimal share a digest — keep behavior-affecting configuration well
  inside that).
- **Migration note:** payloads with non-string mapping keys are no longer
  accepted by `stable_hash`/`canonical_json`; convert keys to strings first.
- **API ambiguity and silent fallbacks removed** (roadmap R022). Passing
  `thread_id`/`resume`/`checkpoint_id`/`metadata` alongside a complete
  `AgentRequest` now raises `ConfigurationError` instead of silently ignoring
  them. `AgentResult.text` supports the documented message shapes (strings,
  mappings with a `text` field, and text-block sequences — joined with newlines)
  instead of silently omitting mapping-shaped messages. `ScopeTree.providers_of`
  raises for a missing scope instead of answering `()` — a valid empty scope and
  a missing scope are different facts. `ReplayMismatch` distinguishes exhaustion
  from a missing key (`reason="exhausted"`/`"missing"`).
- **Migration note:** code that combined a complete `AgentRequest` with
  convenience arguments now raises; move those fields into the request itself.
  Code relying on `providers_of` returning `()` for unknown scope paths must
  handle `ConfigurationError`.
- **Diagnostics and documentation aligned with implementation** (roadmap R023).
  Status and cleanup reports render failures through a structured, redacted
  representation (`description`, `error_type`, scrubbed `error`) instead of raw
  exception strings, and `ReconcileResult.to_dict` accepts a `sanitize`
  callable. The resolver documentation now states the real rule (active plugins
  contribute registrations; selection stays ambiguous without a preference —
  stability comes from incremental reuse). A documentation test maps every
  guarantee G1–G24 to at least one existing test node, and the design, security,
  replay, observability, and hook documentation is synchronized with its
  regression tests.
- **Migration note:** `CleanupFailure.to_dict()` now returns `error_type` plus a
  scrubbed `error` message instead of one combined `"Type: message"` string.
- **Coverage and compatibility gates established** (roadmap R024). CI now
  enforces branch coverage for `src/chassis` at the measured 0.5.0 baseline of
  91% (`pytest --cov=src/chassis --cov-branch`) plus focused per-area floors for
  lifecycle, resolver, registry, security, replay, and telemetry via
  `scripts/coverage_gate.py` — a high global number can no longer hide an
  untested boundary. The lockfile job stays the reproducible development
  baseline, with a minimum-supported-direct-dependencies job
  (`uv lock --resolution lowest-direct`) and a latest-compatible job
  (`uv lock --upgrade`) comparing against it.

## [0.7.0] - 2026-09-22

Gives invoke, stream, replay, hooks, budgets, and telemetry one coherent
execution contract: a shared run lifecycle with full attribution on both agent
paths, semantically complete replay keys and results, replayed tool calls that
follow the live boundary, settled hook semantics with surfaced failures,
validated budgets and tool contracts, isolated telemetry failures, and truthful
runtime identity. Every change that can break a caller from 0.6 is listed in
[migrations](https://github.com/andreolli-davide/chassis/blob/main/docs/migration.md).

### Fixed

- **Agent lookup runs after readiness reconciliation** (roadmap R013).
  `agents.invoke`/`stream` call `ensure_ready()` before resolving the agent
  runtime and logical revision, so an agent installed or replaced after
  `Harness.start()` resolves and runs — its contributions have mounted (and any
  runtime they register exists) before lookup. Generation-owned registrations
  are still selected only after the generation is acquired. Evaluation targets
  resolve logical `AgentSpec` names as well as raw runtime names.
- **Invoke and stream share one run lifecycle** (roadmap R014). Readiness,
  generation acquisition, agent resolution, budget scope, hooks, telemetry,
  snapshot attribution, and cleanup run identically for both paths: the same
  `agent.run` span and snapshot digest are emitted, the parent budget stays
  active through before/after hooks and any child-agent call they make, and
  every streamed event is stamped with the run's logical agent, revision,
  generation, run, and thread attribution (`AgentEvent` gained `thread_id`)
  instead of trusting a custom runtime to attribute its own events. Hook
  failures, agent errors, and cancellation behave identically on both paths.
- **Replay keys and results are semantically complete** (roadmap R015). Model
  boundary keys now include normalized stop sequences and every invocation
  option — temperature, tools, structured output/response format, provider
  options — so requests differing by one semantic option can never collide;
  a value that cannot be canonicalized deterministically (arbitrary objects,
  non-finite floats) raises `ReplayMismatch` instead of being silently omitted.
  Recorded results preserve `llm_output` and per-generation metadata.
  `ReplaySession` gained cursor-aware `has_remaining()`/`peek()` (and replay
  consumption is per key, so out-of-order consumption across keys never skips a
  record); replayed tool calls use the cursor-aware check and fall back live
  once their records are exhausted.
- **Migration note:** model recordings made with earlier versions omit options
  and result metadata from their keys — re-record them for exact matching.
  Code that relied on `has()` implying an unconsumed record should use
  `has_remaining()`.
- **Replayed tool calls follow the live boundary** (roadmap R016). A replayed
  call runs the identical boundary sequence — before hook, authorization,
  approval, budget, and the `tool.execute` span (now tagged `replayed`) — fires
  the after hook for a successful recorded result, and follows the live failure
  shape (error hook, no after hook) for a recorded failure. Replay reuses only
  the recorded semantic result (`content`/`artifact`/`error` stay historical);
  `duration_seconds`, `tool_call_id`, `generation_id`, and the new
  `ToolExecutionResult.run_id` are stamped with the current run's attribution,
  and replaying in a different generation works cleanly.
- **Migration note:** `ToolExecutionResult` gained `run_id` (round-tripped
  through replay payloads). Code that assumed replayed results carried the
  recording's generation/call ids or its recorded duration should read the
  current attribution instead — only the semantic result is historical.
- **Hook semantics settled and failures surfaced** (roadmap R017). `TRANSFORM`
  now *replaces* the payload exactly as documented — keys the returned mapping
  does not carry are removed — and chained transforms each replace the payload
  for the remaining handlers. Hook payloads are deep-frozen (nested mutation
  raises `TypeError` at every depth). `RECORD` handler failures on the tool and
  agent data-plane are surfaced as structured `hook.failure` telemetry events
  (event, handler description, error type — never error text) without failing
  the operation; control-plane failures continue to aggregate into the
  transition's report.
- **Migration note:** a `TRANSFORM` handler that relied on patch semantics (a
  partial mapping merged over the old payload) must now return the complete
  replacement payload — omitted keys are removed. Handlers that mutate nested
  payload containers must copy them first.
- **Budgets and tool contracts validate at construction** (roadmap R018).
  `BudgetLimits` rejects negative and non-finite values and requires integers
  for count dimensions; consumption amounts are validated the same way at every
  charge — fractions on count dimensions raise instead of being truncated
  silently (zero remains a valid boundary). `ToolPolicy` normalizes its
  sequences to tuples, deep-freezes metadata, and requires a positive finite
  `timeout_seconds` when present. An obviously synchronous `ainvoke`
  implementation is rejected at registration, and the executor verifies
  awaitability at the boundary with a typed `ToolExecutionError`. Wall-clock
  enforcement is documented as cooperative — a hard deadline needs a cancellable
  boundary of your own.
- **Migration note:** code that passed fractional/negative/non-finite budget
  amounts or limits, or a non-positive `ToolPolicy.timeout_seconds`, now raises
  `ConfigurationError`; tools with a synchronous `ainvoke` must become
  `async def`.
- **Telemetry failures are isolated and adapter wiring unified** (roadmap R019).
  The new `SafeTelemetry` wrapper contains any backend failure at span enter,
  update, error, exit, and event time — observability can never break runtime
  correctness or suppress another backend — with failures kept visible through
  a `failures` counter and the `chassis.telemetry` logger. `TeeTelemetry` runs
  every backend through it and exposes a `failures` total; the harness wires one
  `SafeTelemetry(RedactingTelemetry(...))` chain around every configured
  backend. Registered runtimes that expose `bind_harness_services` (such as
  `LangGraphAgent`) adopt the harness telemetry and redaction at registration
  unless they were constructed with an explicit override.
- **Runtime snapshots and graph cache validation corrected** (roadmap R020).
  `RuntimeSnapshot.agent_runtime` now reports the identity of the *selected*
  `AgentRuntime` (`runtime_kind` when the runtime declares one, else its class
  name) — custom runtimes are never labelled `langgraph`, and snapshots built
  without a runtime report `unknown`. `composition_metadata` resolves the same
  identity for evaluation experiments. `GraphCache` validates `max_entries`
  (non-negative integer; `0` explicitly disables caching) and documents that
  topology or captured static inputs invisible to the cache key require an
  `AgentDefinition.version` change, with a guide warning preferring
  harness-bound runtime tools over captured static implementations.
- **Migration note:** code that asserted `snapshot.agent_runtime == "langgraph"`
  for custom runtimes must read the runtime's own identity; snapshots without an
  agent run now report `unknown` instead of `langgraph`.

## [0.6.0] - 2026-09-22

Makes composition transactional and generation-safe: identity-keyed
registrations, exact capability contract binding, runtime enforcement of
composition visibility, deeply immutable published state, transactional
`AgentSpec` materialization and declarative configuration, and validated
composition-tree ownership, paths, and names. Every change that can break a
caller from 0.5.1 is listed in
[migrations](https://github.com/andreolli-davide/chassis/blob/main/docs/migration.md).

### Fixed

- **Tool and agent registrations are keyed by identity** (roadmap R006). Every
  registration now carries an immutable registration id, with name indexes
  maintained separately from the store, so old and new generations can reference
  same-named registrations concurrently and each generation's tool view selects
  the registrations owned by its own instances. Cleanup is identity-checked: an
  old scope closing can never unregister its successor, in either release order,
  and hot replacement of same-named tools and runtimes works while an old run
  remains leased (the leased run keeps the objects it resolved).
- **Migration note:** a duplicate tool *name* is no longer rejected at
  registration; a candidate generation that would expose two same-named tools is
  rejected at publication with `ConfigurationError` and rolled back. Agent
  runtime names stay explicitly unique unless `replace=True`.
  `ToolRegistry.unregister`/`AgentRegistry.unregister` gained keyword-only
  `owner_id`/`scope_id` filters so a plugin's early unregister touches only its
  own registration.
- **Requirements bind the exact selected capability contract** (roadmap R007).
  `RequirementResolution` now carries the selected contract key
  (`provider_key`) alongside the provider identity and version, and
  materialization filters registrations with the original requirement
  predicate instead of selecting by capability name and provider instance —
  registration order and random ids no longer decide which registration a
  consumer gets (snapshot and registry ordering are insertion-stable).
  Application provisions are keyed by the full `CapabilityKey`, so `database@1`
  and `database@2` provisions coexist. Contract-generation inference from a
  specifier now proves a single major before pinning: `>1.9,<3`, `>=1`, and
  similar open or multi-major ranges stay generation-neutral instead of being
  pinned to the lower-bound major. `PluginManifest.provides` accepts a sequence
  of versions for multi-contract providers.
- **Migration note:** a requirement like `>=2` no longer pins `@2`; pass an
  explicit `api_version` when a specific contract generation is required.
  `Harness.provide`/`withdraw` are keyed by `CapabilityKey`; `withdraw` with a
  name removes every contract generation of that name. `PluginManifest.provides`
  values may now be `str | tuple[str, ...]`.
- **AgentSpec composition visibility is enforced at runtime** (roadmap R008).
  Runs now receive a capability snapshot filtered by the acquired
  `ResolvedScope.visible` registrations, so `HarnessRunContext.require_capability()`
  and graph build-time capability versions observe exactly the composed view:
  hidden, narrowed, and sibling-local capabilities are invisible in both the
  invoke and stream paths, while inherited and versioned ones resolve as
  composed. This is isolation of composition, not security authorization. An
  unknown scope path is now rejected with `ConfigurationError` instead of
  silently exposing an empty tool view (`Harness.scoped_capabilities` is new).
- **Migration note:** run code that reached capabilities outside its agent's
  composition view will now raise `CapabilityNotFound`/`CapabilityVersionMismatch`;
  widen the spec's `capabilities` view where that access is intended. Lookups
  against an undeclared scope path raise instead of returning empty results.
- **Published state is deeply immutable** (roadmap R009). Recursive
  copy-and-freeze now applies at every publication boundary — plugin config and
  manifest metadata, scopes and resolved scopes, generation metadata, capability
  and tool registrations, `ToolPolicy`, run metadata (`AgentRequest`,
  `AgentResult`, `HarnessRunContext`), and runtime snapshots — so nested mutation
  of any published container raises `TypeError` and author-owned dicts, lists,
  and sets can never alias published state. Semantic fingerprints are computed
  from exactly the frozen state a generation exposes. The intentional exception
  is documented: executable provider and tool objects are live runtime objects —
  their contracts and metadata are frozen, not their internals.
- **Migration note:** code that mutated nested containers obtained from
  `entry.config`, `manifest.metadata`, `scope.metadata`, `generation.metadata`,
  registration/tool metadata, `ToolPolicy.metadata`, run metadata, or snapshot
  metadata must mutate its own copies instead; published containers now raise at
  every depth.
- **AgentSpec materialization is transactional and owner-safe** (roadmap R010).
  Contributions are staged and validated before any desired state is mutated,
  and a failed materialization restores the exact previous scope and registry
  state — including entries a replacement had swapped. A revision that moves to
  a new scope withdraws the old one only after the new one materialized, so a
  failed replacement never destroys the active revision. `install` now refuses
  to take over a pre-existing user-owned composition scope instead of rewriting
  it, an agent-owned entry id squatted by a foreign entry is rejected, reserved
  metadata keys (`chassis.agent`, `chassis.agent_revision`) are rejected at
  `AgentSpec` construction, and withdrawal removes only the state a revision
  owns.
- **Migration note:** code that pointed an `AgentSpec.scope` at an existing
  user-owned scope relied on implicit takeover; declare the agent's own scope
  (the default `/agents/<name>`), or empty the user scope first. Specs carrying
  reserved metadata keys now fail construction.
- **Declarative configuration applies atomically** (roadmap R011). The complete
  configuration is parsed, migrated, validated, and catalog-resolved before any
  desired state is mutated; entries, config-owned preferences, and the stored
  config commit as one transaction, and a failure (unknown plugin, invalid
  preference, or a failing entry change) restores the exact previous state
  including partial removals. Unsupported schema versions are rejected by an
  explicit migration dispatcher (and by `HarnessConfig` itself), config-owned
  provider preferences are replaced wholesale on every application, and
  programmatic `prefer_provider` preferences are stored separately and take
  precedence over config-owned ones with the same key.
- **Migration note:** a re-applied configuration now *replaces* its provider
  preferences — a preference omitted from the new document no longer lingers
  (re-declare it, or set it programmatically with `prefer_provider`, which now
  outranks the document). Configurations with a schema `version` other than `1`
  are rejected instead of being accepted silently.
- **Composition-tree ownership and naming are validated** (roadmap R012). A
  parent scope belonging to another `CompositionTree` is rejected, and every
  tree/`AgentSpec`/harness API accepts the same canonical absolute scope paths    (no relative forms, empty or untrimmed segments, `.`/`..`, or trailing
  slashes) via one shared `chassis.core.paths.canonical_scope_path`. Whitespace-    only or untrimmed capability, permission, catalog, entry, and agent-runtime
  names are rejected, and config/manifest schema versions must be non-negative.
- **Migration note:** non-canonical scope paths (relative, `"//"-containing`,
  untrimmed, or trailing-slash forms) and whitespace-padded names that were
  previously accepted silently now raise `ConfigurationError`; trim and
  canonicalize before calling. Negative `config_version` values are rejected.

## [0.5.1] - 2026-09-22

### Fixed

- **Generation lease accounting is authoritative** (roadmap R001). The idle event
  is now cleared on the `0 -> 1` lease transition, so a `wait_idle()` that joins
  after a reacquisition waits for the current cycle instead of observing a stale
  idle signal. Releasing a lease id that is not outstanding — a forged id or a
  duplicate release — raises the new `UnknownLeaseError` and never alters the
  accounting. `GenerationManager.retire()` refuses a generation with outstanding
  leases outside terminal shutdown; the explicit `begin_shutdown()` transition is
  the only context where a still-leased generation may retire, and late releases
  against it stay exact. `GenerationManager(history_limit=...)` now rejects a
  negative value with `ConfigurationError`.
- **Migration note:** callers that released a lease twice or released an unknown
  lease id previously corrupted the lease count silently; they now receive
  `UnknownLeaseError`. Catch it only if you intentionally tolerate buggy release
  paths; correct callers are unaffected.
- **Policy and secret resolution fails closed** (roadmap R002). Registered policy
  and secret providers are resolved as explicit system requirements: the
  configured default is used only when the generation registers no provider,
  several eligible providers are rejected unless an explicit
  `prefer_provider(...)` preference selects one, and a registration that does not
  implement the contract is a provider failure. Resolution failures deny the tool
  call (and deny secret reads) instead of falling back to `AllowAllPolicy` or the
  environment-backed default, and a policy provider that raises while deciding
  denies the call with `PolicyDenied`, preserving the original exception only as
  an internal cause.
- **One end-to-end redaction boundary** (roadmap R003). A single harness-owned
  `SecretRedactor` now covers replay, diagnostics, tool execution, agent
  execution, LangGraph run configuration, recording telemetry, and LangSmith.
  Telemetry backends receive pre-scrubbed attributes, updates, events, and errors
  through the new `RedactingTelemetry` wrapper, and `LangSmithSpan` scrubs
  attribute updates on its own path. Redaction is recursive over nested mappings
  and sequences and replaces whole values under sensitive key names on request
  and response paths. Policy-denial reasons and agent runtime exceptions are
  sanitized before crossing a public, hook, diagnostic, replay, or telemetry
  boundary, with the original exception preserved only as an internal cause.
  `EffectCleanupError` and cleanup reports render sanitized text while keeping
  the original exceptions as structured detail. Tool, capability, and
  replay-session metadata are scrubbed, and a `ReplaySession` attached to a
  harness adopts the harness redactor and is re-scrubbed (`bind_redactor`).
- **Migration note:** an agent runtime failure other than a `ChassisError` now
  surfaces as `AgentExecutionError` (cause preserved) instead of the raw runtime
  exception. New: `AgentExecutionError`, `RedactingTelemetry`,
  `ReplaySession.bind_redactor`, `LangSmithSpan(run, redactor=...)`.
- **Secret values shorter than four characters are now protected** like any
  other; only the empty string is untrackable. Callers relying on `add()`
  refusing short values must stop doing so.
- **Publication validates actual capability registrations** (roadmap R004).
  Before a candidate generation is published, every mounted plugin's effective
  registrations are compared with the contracts its manifest promises, and the
  resolution fixpoint is re-checked against the registrations consumers actually
  got. A provider that promised a capability but registered none — including in
  the first generation — a registration on the wrong contract generation, or a
  registered version no consumer's requirement accepts now rolls the entire
  candidate back with a structured `PluginContractError` naming the provider,
  promised contract, actual registrations, affected consumers, and rollback
  result. Previously such a candidate published silently and the gap surfaced
  only at run time.
- **Migration note:** plugin fixtures that declare `provides` without registering
  the capability now fail at publication; register what you declare or narrow
  the manifest.
- **Failed scope entry and failed plugin cleanup are reversible** (roadmap R005).
  A `__enter__`/`__aenter__` that fails no longer leaves an effect record the
  scope cannot revert. Rollback cleanup failures are aggregated into the raised
  `PluginSetupError` (`cleanup_failures`, with a count in the structured
  context) and into `last_cleanup_failures` instead of being hidden inside the
  failed scope. Failed plugin instances stay inspectable and retryable while
  their entry is desired, and are reclaimed once it is not — no orphaned
  `FAILED` instances remain resident. Owned tasks that resist cancellation past
  the shutdown timeout are reported and stay visible (`Scope.stragglers`), and
  such a scope is never presented as fully disposed (`Scope.fully_disposed`,
  plus `stragglers`/`fully_disposed` in `Scope.to_dict()`).
- **Migration note:** `PluginSetupError` gained `cleanup_failures=` and reports
  a `cleanup_failures` count in its context; a scope that failed to stop all its
  tasks now reports `fully_disposed: False` where it previously claimed a clean
  close.

### Documentation

- Added the [release roadmap](docs/roadmap.md), mapping every finding from the 0.5.0
  repository audit to the planned 0.5.1–0.8.0 releases, with acceptance criteria,
  dependencies, exit gates, and permanent traceability.
- Marked the known 0.5.0 enforcement gaps in the design guarantees and security
  guide so the documentation does not overstate current secret, lifecycle, and
  transactional protections while remediation is pending.

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
