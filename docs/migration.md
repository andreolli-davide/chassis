# Migrating between versions

Chassis is pre-1.0, and each minor release uses that freedom to remove ambiguity.
Each section lists every change that can break a caller from the previous version,
why it was made, and what to do instead. Lifecycle behaviour is unchanged across
these releases: published generations are still immutable, publication is still
transactional, and logical unload is still distinct from physical disposal.

- [0.5 → 0.5.1](#05-051): fail-closed policy and secrets, redaction boundary,
  authoritative lease accounting, publication contracts
- [0.4 → 0.5](#04-05): agent composition, agent revisions, tool visibility
- [0.3 → 0.4](#03-04): incremental composition, semantic identity, reuse diagnostics
- [0.2 → 0.3](#02-03): composition scopes, explain and diff diagnostics
- [0.1 → 0.2](#01-02): optional extras, tool protocol, lease identity, budgets

## 0.5 → 0.5.1

0.5.1 is a security and lifetime hotfix: it closes the audit gaps in G3, G6, and
G11. No API was removed, but behaviours that were silently wrong now fail loudly.

### Stale lease releases are rejected

Releasing an unknown or already released lease id previously corrupted the lease
count silently; it now raises `UnknownLeaseError` and never alters the
accounting. `GenerationManager.retire()` refuses a generation with outstanding
leases except after the terminal `begin_shutdown()` transition, and a negative
`history_limit` is rejected with `ConfigurationError`.

### Policy and secret resolution fails closed

Registered policy and secret providers are resolved as explicit system
requirements. The configured default applies only when the composition registers
no provider at all. Several eligible providers are rejected unless an explicit
`prefer_provider(...)` preference selects one — such runs deny tool calls and
secret reads instead of silently widening to `AllowAllPolicy` — and a policy
provider that raises while deciding denies the call (`PolicyDenied`, with the
original exception as internal cause).

### Agent runtime failures are wrapped

An agent runtime failure other than a `ChassisError` now surfaces as
`AgentExecutionError`, with the original exception preserved as `__cause__`.
Catch `AgentExecutionError` (or `ChassisError`) instead of the runtime's own
exception types.

### Redaction and publication are stricter

`SecretRedactor.add` now protects every non-empty value, including values shorter
than four characters; `redact_value` also replaces whole values under sensitive
key names at every depth. A plugin manifest `provides` entry that registers
nothing (or registers an incompatible contract) now fails publication with
`PluginContractError` and rolls the candidate back — register what you declare.
`PluginSetupError` gained `cleanup_failures` (rollback cleanup errors are no
longer hidden), and a scope that could not stop all its owned tasks reports
`fully_disposed: False` instead of claiming a clean close.

### Additive APIs

New in 0.5.1: `AgentExecutionError`, `UnknownLeaseError`, `PluginContractError`,
`RedactingTelemetry`, `ReplaySession.bind_redactor`, `Scope.stragglers`,
`Scope.fully_disposed`, and `LangSmithSpan(run, redactor=...)`.

## 0.4 → 0.5

0.5 adds first-class, versioned agent composition on top of the existing kernel. It
is additive: every 0.4 guarantee still holds, and a composition with no `AgentSpec`
behaves exactly as before. Two snapshot and attribution details change.

### `RuntimeSnapshot` gained `agent_revision`

`RuntimeSnapshot` now carries the agent revision a run executed against, and
`to_dict()` (and therefore `digest()`) includes it. A digest recorded with 0.4 will
not equal the digest of the same composition in 0.5; re-baseline stored snapshots,
as for 0.3 and 0.4. `semantic_digest()` and `physical_digest()` keep their meaning,
and `public_composition_digest()` is an additive alias for `semantic_digest()`.

### Run attribution stamps the logical agent

`harness.agents.invoke`/`stream` stamp the logical agent name and revision on the
result (and streamed events), so a runtime referenced by `AgentSpec.runtime_ref`
reports the agent the caller selected rather than its own internal name. An agent
registered only as a runtime is unchanged.

### Additive APIs

Nothing was removed and no existing signature changed otherwise. New in 0.5:

- `chassis.agents.AgentSpec`, `AgentRevision`, `AgentRegistry` (spec registry
  methods `install`/`replace`/`remove`/`spec`/`active_spec`/`revisions`/`specs`/
  `history`), `AgentNotFound`, `AgentRetired`;
- `HarnessRunContext.agent_revision` and `.agent_identity`; `AgentResult.agent_revision`
  and `AgentEvent.agent_revision`;
- `RuntimeSnapshot.agent_revision`, `.agent_identity`, `.public_composition_digest()`;
- `PluginManifest.implementation_revision` (optional; absent keeps the previous
  implementation fingerprint);
- composition tool visibility: `CompositionScope.tools`/`select_tools`/
  `expose_all_tools`, `CompositionTree.child(..., tools=...)`, `ResolvedScope.tools`/
  `local_tools`/`inherited_tools`/`visible_tools`, `Harness.tool_snapshot(..., scope=)`,
  `Harness.run_environment(..., scope=)`, and `ScopeExplanation.visible_tools`;
- `harness.diagnostics.explain_agent(...)` and `.diff_agents(...)`, with
  `AgentExplanation` and `AgentDiff`.

### Agent composition checklist

- Publish an agent with `harness.agents.install(AgentSpec(...))`; a revision that was
  already published with different content is rejected, so bump the revision.
- Hold `runtime_ref` stable if you do not want a revision change to change the
  execution runtime.
- If you registered an agent as a runtime and want revision attribution, add a spec
  whose `runtime_ref` names that runtime.
- Tool names remain process-global: two agent scopes cannot contribute the same tool
  name; share a tool plugin at an ancestor scope instead.

## 0.3 → 0.4

0.4 makes composition incremental: unchanged runtime nodes are reused across
generations, and a change rebuilds only the nodes whose semantic inputs changed.
The lifecycle is unchanged — generations are still immutable, publication is still
transactional, and disposal still follows reachability — but one behaviour changed
on purpose, and the runtime snapshot gained fields.

### A consumer is rebuilt when its selected provider changes

In 0.3, `_materialize` reused an instance whenever its entry revision was
unchanged. A consumer whose *selected provider* changed was therefore carried into
the new generation with the capability objects it captured during `setup`,
pointing at a provider that was on its way out. 0.4 decides reuse from **semantic
identity**, which includes the resolved dependency binding, so such a consumer is
rebuilt and re-resolves against the provider the new generation publishes. The diff
reports it as `REWIRED` (only its bindings changed), and its `setup` runs again.

What this means in practice:

- a consumer that reaches a provider no longer survives that provider's replacement
  as a live instance; its teardown and setup run once more, and the resource is
  re-created. If your plugin's `setup` is expensive, that cost is now paid exactly
  when the provider it uses changes;
- unrelated nodes are still reused, so a localized change does not remount the whole
  composition (see `diagnostics.analyze_impact`);
- a consumer whose provider is replaced by a *semantically identical* one is rebuilt
  as a new instance and reported as `UNCHANGED` (semantically identical), not
  `REUSED`.

This is the change required by the new guarantee G18 and is covered by
`tests/composition/test_impact_analysis.py`.

### `RuntimeSnapshot` separates semantic and physical identity

`RuntimeSnapshot` gained two fields and two digests:

- `runtime_instance_ids` — the runtime instances the generation was published with,
  included in `to_dict()` and therefore in `digest()`;
- `semantic_scopes` — the scope topology with provider *entry* ids instead of
  runtime instance ids, used by `semantic_composition()`/`semantic_digest()`;
- `semantic_digest()` hashes only the semantic composition (plugins, capabilities,
  redacted config, dependency edges, tool contracts, semantic scope tree), so two
  semantically equivalent generations that were materialised separately share it;
- `physical_digest()` hashes the runtime instance ids.

A digest recorded with 0.3 will not equal the digest of the same composition in
0.4, because `to_dict()` gained `runtime_instance_ids`. Re-baseline stored
snapshots. `config_hash` is unchanged and still redacted; a secret-only
configuration change is invisible in `semantic_digest()` but still forces a
rebuild.

### Additive APIs

Nothing was removed and no existing signature changed otherwise. New in 0.4:

- `chassis.composition` re-exports `SemanticIdentity`, `DependencyBinding`,
  `ReuseDecision`, `ReuseReason`, `NodeImpact`, `ImpactAnalysis`;
- `PluginInstance.semantic_identity` and `RuntimeGeneration.identities`
  (`identity_for(entry_id)`);
- `ReconcileResult.impact`;
- `harness.diagnostics.analyze_impact(old, new)` and
  `.explain_reuse(old, new, node)`; `GenerationDiff.nodes` and
  `GenerationDiff.by_decision(decision)`; `ReuseExplanation`;
- `GenerationPressureReport.resources` (`ResourceReachability`) and the
  `chassis.resources.shared` gauge;
- `PluginRegistry.mount(..., supersede=True)`, used by reconciliation to mount a
  fresh instance for an unchanged revision without disposing a still-reachable
  predecessor. Callers other than the harness do not need it.

### Incremental composition checklist

- If you replace a provider, expect its consumers to be rebuilt; prefer a stable
  entry id for the provider so only the provider's own nodes are rebuilt when
  behaviour changes.
- If a plugin's `setup` performs an expensive side effect, it now runs again when
  that plugin's configured behaviour changes — including a configuration change.
- If you store snapshots, re-baseline `digest()` and start using `semantic_digest()`
  when you mean "the same composition" rather than "the same record".
- Unchanged metadata, scope metadata values, and preferences that select the same
  provider do not force a rebuild.

## 0.2 → 0.3

0.3 is additive for existing code: a composition with no declared scopes behaves
exactly as it did, and every 0.2 guarantee still holds. Two changes are worth
checking against.

### `RuntimeSnapshot.scopes` is part of the digest

A snapshot now carries the resolved scope tree — scope topology, the capability
view in effect, the local providers of each scope, and the selected provider of
every requirement — and that payload participates in `RuntimeSnapshot.digest()`.
The decision is deliberate: scope topology and resolution are observable through the
generation a run acquires, so two generations that differ there must not share a
digest.

What this means in practice:

- a digest recorded with 0.2 will not equal the digest of the same composition in
  0.3, because the snapshot gained a field. Recorded LangSmith metadata, evaluation
  metadata, and any stored snapshot comparisons should be re-baselined;
- a no-op reconcile still reuses the generation and reproduces the identical digest,
  so digest stability within a version is unchanged;
- `snapshot.scopes` never contains configuration or scope metadata values.

### Provider preference keys gained a scope form

`Harness.prefer_provider` keeps its 0.2 behaviour and gained keyword arguments:

```python
harness.prefer_provider("database", "postgres")                     # global (0.2 behaviour)
harness.prefer_provider("database", "postgres", consumer="agent")   # "<entry>:<capability>"
harness.prefer_provider("database", "postgres", scope="/research")  # "scope:<path>:<capability>"
```

`prefer_provider("agent:database", "postgres")` (the 0.2 positional form) is now
rejected by the type signature: pass `consumer="agent"` instead. Configuration-level
`provider_preferences` and per-entry `provider_preference` are unchanged.

### Additive APIs

Nothing was removed and no existing signature changed otherwise. New in 0.3:

- `harness.composition` — the desired-state tree of composition scopes
  (`chassis.composition.CompositionTree`, `CompositionScope`);
- `Harness.install(..., scope=...)`, `CompositionScope.install(...)`,
  `CompositionScope.require(...)`, `.restrict(...)`;
- `RuntimeGeneration.scopes` (`chassis.composition.ScopeTree` of `ResolvedScope`);
- `harness.diagnostics.scopes()`, `.explain_requirement(...)`, `.explain_scope(...)`,
  `.diff_generations(...)`;
- structured types `chassis.plugins.resolver.ProviderAssessment`,
  `ScopePlan`, `RequirementResolution` provenance fields, and the diagnostics
  `RequirementExplanation`, `ScopeExplanation`, `GenerationDiff`, `CompositionChange`.

`ResolutionPlan.scopes` is populated whenever the harness resolves; the resolver
also accepts `scopes=` directly. A plan produced without scopes still has a root
scope, so `plan.scope_for("/")` is never `None`.

### Scoped composition checklist

- Entries installed through `harness.install(...)` before 0.3 are root-scope entries;
  no migration is needed to keep them there.
- A child scope's consumers see their ancestors' providers; if a 0.2 composition now
  lives inside one scope alongside a sibling, confirm sibling isolation is what you
  want (it is the guarantee, not a configuration).
- If two valid providers (one local, one inherited) now make a requirement
  `ambiguous`, that is the same explicit-ambiguity rule as 0.2 applied across scopes:
  declare a preference rather than expecting a local provider to shadow.

## 0.1 → 0.2

### Installation

The core no longer depends on `langgraph`, `langchain-core`, or `langsmith`.

```bash
# 0.1
pip install chassis-harness

# 0.2
pip install chassis-harness                        # core only
pip install "chassis-harness[langgraph]"           # LangGraph adapter + langchain-core
pip install "chassis-harness[langsmith]"           # LangSmith telemetry + evaluation
```

If you use `chassis.langgraph`, `chassis.replay.ReplayChatModel`, `FakeChatModel`,
`fake_tool`, or any `langchain-core` model or tool, install the `langgraph` extra.
If you use `LangSmithTelemetry` with tracing enabled, or `evaluate_agent`, install
the `langsmith` extra. Importing those without the extra raises a `MissingExtraError`
that names the extra to install.

### `ToolSnapshot.to_langchain_tools()` → `to_tools()`

The core no longer names a specific tool library, so the accessor no longer does
either. The returned objects are unchanged.

```python
# 0.1
tools = snapshot.to_langchain_tools()

# 0.2
tools = snapshot.to_tools()
```

A tool registered with the harness must now satisfy the structural
`chassis.tools.Tool` protocol — a `name`, a `description`, and an awaitable
`ainvoke` — rather than being an instance of `langchain_core.tools.BaseTool`. A
`BaseTool` satisfies the protocol unchanged, so existing tools keep working; the
change only removes the import-time dependency.

### `GenerationManager.acquire()` / `release()` → `acquire_lease()` / `release_lease()`

Leases now carry identity so that lease *age* is authoritative. `Harness.acquire()`
is unchanged: it still yields the acquired `RuntimeGeneration`.

```python
# 0.1
generation = manager.acquire()
manager.release(generation)

# 0.2
lease = manager.acquire_lease()
manager.release_lease(lease)          # lease.generation is the generation
```

Most code uses `async with harness.acquire() as generation:` and needs no change.

### Budget enforcement is now explicit

`BudgetLimits(...)` and the enforced/accounted split are unchanged in shape, but the
API now states which is which:

- `wall_clock_seconds`, `tool_calls`, `child_runs` are **enforced**: a configured
  limit is a guarantee at a Chassis-owned boundary.
- `model_calls`, `tokens`, `estimated_cost` are **accounted**: a configured limit is
  intent, and holds only when the integration that owns the call reports usage with
  `run_context.budget.record(model_calls=…, tokens=…, estimated_cost=…)`.

If you configured a token or cost limit in 0.1 expecting automatic enforcement,
0.2 makes the gap visible: `BudgetDimension.TOKENS.enforcement`,
`BudgetLimits.accounted_dimensions()`, `governor.to_dict()["enforcement"]`, and
`harness.diagnostics.budgets()` all report `accounted`. Add a `record(...)` call at
the point where the model response is received. See
[Budgets](plugin-author-guide.md#budgets) for a worked example.

### Nothing removed from the lifecycle

`Harness`, `Scope`, `Plugin`, plugin manifests, the resolver, reconciliation,
`RuntimeGeneration`, run contexts, and diagnostics keep their 0.1 shape. Everything
new — generation pressure, lease age, budget enforcement metadata, the `Tool`
protocol — is additive unless listed above.
