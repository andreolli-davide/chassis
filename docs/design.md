# Design: guarantees, and what is deliberately absent

This page states what Chassis promises. It is short on purpose: every guarantee
below is enforced by tests, and the tests are the specification of record.

!!! warning "Known 0.5.0 audit gaps"
    A repository audit found reproducible gaps in the current enforcement of G3,
    G4, G6, G11, G16, G23, and G24. These guarantees remain the target contracts,
    but must not be treated as fully established by version 0.5.0. The ordered fixes,
    regression requirements, and release gates are tracked in the
    [release roadmap](roadmap.md). Security and lifetime repairs are targeted first
    in 0.5.1; the remaining transactional repairs are targeted in 0.6.0.

## The proposition

> Agent runtime composition changes over time while active runs retain a coherent
> environment, dependencies react correctly, resources have explicit ownership, and
> obsolete components are disposed only when no longer reachable.

## Guarantees

| # | Guarantee | Enforced by |
| --- | --- | --- |
| G1 | Every harness-managed effect has exactly one owning scope | `core/scope.py`, `tests/core/test_scope.py` |
| G2 | A failed plugin setup reverts every effect that setup created | `plugins/registry.py`, `tests/plugins/test_lifecycle.py` |
| G3 | An active composition never contains a plugin whose selected provider is absent or incompatible | `plugins/resolver.py`, `tests/plugins/test_resolver.py` |
| G4 | A published generation is never mutated in a way visible to existing runs | `core/generation.py`, `tests/integration/test_generation_lifecycle.py` |
| G5 | A run acquires either the old complete generation or the new one — never a partial candidate | `core/generations.py` |
| G6 | A plugin scope is never physically disposed while a live generation can reach it | `harness.py` reclamation, `tests/concurrency/test_generation_concurrency.py` |
| G7 | A plugin referenced by several live generations survives until all of them retire | same, plus `tests/integration/…::test_shared_plugin_survives_until_every_generation_releases_it` |
| G8 | Normal model/tool execution never takes the control-plane lock | `harness.py`, `tests/integration/…::test_data_plane_is_not_blocked_by_an_in_flight_reconcile` |
| G9 | LangGraph owns graph durability; Chassis never duplicates checkpointing | `langgraph/`, no second checkpoint system exists |
| G10 | In-process plugins are trusted code; policy is not sandboxing | `docs/security.md` |
| G11 | Secret values never enter snapshots, diagnostics, replay records, traces, or public error strings | `secrets/redaction.py`, `tests/secrets`, `tests/test_evaluation.py` |
| G12 | Equivalent canonical desired state resolves to equivalent providers and ordering | `plugins/resolver.py`, `tests/capabilities` |
| G13 | Generation liveness and lease age are read from authoritative runtime state, never derived from the bounded diagnostics history | `core/generations.py`, `diagnostics.py`, `tests/generations/test_generation_pressure.py` |
| G14 | A configured budget limit states whether Chassis enforces it or an integration must account for it | `budget/models.py`, `tests/budget/test_budget_semantics.py` |
| G15 | A scope observes only its own local composition, composition inherited from its ancestors, and composition its capability view permits; sibling-local composition is not implicitly visible | `plugins/resolver.py`, `composition.py`, `tests/composition/test_scopes.py` |
| G16 | The scope tree and every composition-affecting field of a published generation are immutable; scope-affecting changes become visible only through a new generation | `composition.py`, `core/generation.py`, `tests/composition/test_scopes.py` |
| G17 | Every resolved requirement has an authoritative provenance record (selected provider, origin scope, selection reason) and every unresolved requirement has an authoritative reason | `plugins/resolver.py`, `diagnostics.py`, `tests/composition/test_scope_diagnostics.py` |
| G18 | A runtime node is shared across generations only when its semantic identity proves reuse cannot change observable behaviour | `core/identity.py`, `harness.py`, `tests/composition/test_semantic_identity.py` |
| G19 | A shared runtime resource is not disposed while any live generation can reach it | `core/generations.py`, `harness.py`, `tests/composition/test_structural_sharing.py` |
| G20 | Unaffected, semantically identical nodes are eligible for reuse, and Chassis reports reuse only for a node whose reuse safety it established | `core/identity.py`, `diagnostics.py`, `tests/composition/test_reuse_diagnostics.py` |
| G21 | A published agent revision is immutable; any composition-affecting change creates a new revision | `agents.py`, `tests/agents/test_agent_registry.py` |
| G22 | A run remains associated with the agent revision selected when it started, and with the generation it acquired | `agents.py`, `runtime.py`, `tests/agents/test_invocation.py` |
| G23 | Agent composition is materialized through the same scoped resolver, ownership, semantic identity, and generation publication machinery as all other composition | `agents.py`, `composition.py`, `tests/agents/test_agent_lifecycle.py` |
| G24 | Agent tool and capability visibility is composition, not authorization; it grants no user or organization authority | `docs/agent-composition.md`, `docs/security.md` |

## Decisions worth knowing

- **Agent composition is a thin layer, not a framework.** `AgentSpec` describes what
  composition an agent sees and materializes into the existing composition-scope
  primitives; it never executes, plans, remembers, or authorizes. `AgentDefinition`
  stays the engine-specific graph-build contract, reached only through a logical
  `runtime_ref` (G23). See [agent-composition.md](agent-composition.md).
- **A revision is an explicit author declaration.** Publishing a new revision does
  not require — or imply — a semantic change, and two revisions that materialize to
  an equivalent composition are not collapsed. Reuse still follows semantic identity,
  so an unchanged contribution is not rebuilt across a revision change (G21, G20).

- **Immutable generations instead of hot mutation.** Reconfiguration builds a new
  generation and publishes it atomically; old generations drain. In-place mutation of
  a running composition cannot be made safe while older runs depend on the previous
  semantics.
- **Logical unload ≠ physical disposal.** Removing a plugin means "absent from the
  next generation"; its scope closes only when no live generation reaches it. See
  [lifecycle.md](lifecycle.md).
- **`AsyncExitStack` for reversible effects.** Ownership is a stack discipline, not a
  registry of `unregister_*` calls that plugin authors must remember.
- **Upstream primitives are reused, not wrapped.** In the LangGraph integration, chat
  models, `Runnable`, `BaseTool`, and `StructuredTool` stay `langchain-core` objects;
  the harness adds only metadata and lifecycle semantics (permissions, side effects,
  idempotency, timeouts, cost class, approval). The core stores tools against a
  structural `Tool` protocol, so it never imports a tool library and can compose any
  object that exposes a name, a description, and an async invoke.
- **Build-time vs runtime-bound inputs.** The graph cache key covers structure, state
  schema, static tools, and build-time capabilities. Changing a model, database,
  policy, secret provider, or tenant reuses the compiled graph.
- **Pressure is observable, not enforced.** Generations stay alive while runs hold
  leases, because a run must keep the composition it acquired. `generation_pressure()`
  reports that state rather than imposing a limit: a loitering generation becomes
  visible and attributable, never silently reclaimed ([lifecycle.md](lifecycle.md)).
- **Budget enforcement is stated per dimension.** A dimension Chassis observes at its
  own boundary is a guarantee; one produced inside the execution engine is intent
  until the integration reports it ([plugin-author-guide.md](plugin-author-guide.md#budgets)).
- **Replay is boundary-based.** Tool and model boundaries replay; snapshots,
  lifecycle events, and interrupt values are recorded as attribution. Clocks,
  networks, databases, and filesystems are explicitly *not* virtualized
  ([replay.md](replay.md)).
- **Configuration changes are replacements.** A changed config becomes `REPLACE`
  rather than an in-place `RECONFIGURE`, because mutating a live instance cannot be
  made safe for generations that still hold it.
- **Composition scopes are derived views, not mutable overlays.** A scope is
  control-plane desired state; resolution materializes it into an immutable tree
  published with the generation. Scope topology is part of composition identity, so
  adding a scope or narrowing a view publishes a new generation even when no
  instance changed (G16).
- **Scoped visibility is inheritance-plus-narrowing, and ambiguity stays explicit.**
  A consumer sees its own and its ancestors' providers, filtered by the intersection
  of the capability views along its path. A valid local provider does not silently
  shadow a valid inherited one: the requirement stays `ambiguous` until a preference
  selects one, which is the same rule flat composition has always used (G15).
  Capability narrowing is composition visibility, not authorization.
- **Scoped ownership reuses the existing lifecycle.** A composition scope owns the
  entries it declares; the physical owner of a registration remains the plugin
  instance's `Scope`. Rollback, reachability, and disposal are unchanged, so there is
  no second teardown system.
- **Provenance is authoritative structure, not parsed logs.** Each requirement
  resolution records its candidates, their origin scopes, visibility, eligibility,
  rejection reasons, the selected provider, and the selection reason. Explain and
  diff APIs read that structure (G17).
- **Reuse is a semantic proof, not a heuristic.** A node is carried across
  generations only when its implementation, capability contracts, effective
  configuration, scope path, and resolved dependency bindings are all unchanged.
  The proof is conservative: anything Chassis cannot establish is rebuilt
  (G18). See [incremental-composition.md](incremental-composition.md).
- **Sharing is not shared mutability.** A reused instance is never reconfigured in
  place; a change that would require it rebuilds the node instead, so a published
  generation still never observes a composition change after publication.
- **Lifetime follows reachability, not ownership transfer.** A shared resource is
  disposed only when no live generation can reach it, using the same lease and
  reachability machinery as before; incremental reuse added no second lifetime
  system (G19).
- **Semantic sameness and physical reuse are separate facts.** The diff and the
  snapshot report them separately, and `REUSED` is only claimed when the exact
  runtime instance was retained (G20).
- **Impact follows dependency bindings, not scope membership.** A scope narrows the
  search for affected nodes; it never replaces the dependency analysis. A provider
  change rebuilds exactly the consumers that reach it, including across scopes.

## Deliberate absences

- no graph engine — LangGraph executes, Chassis composes;
- no competing experiment platform — LangSmith stays the evaluation home;
- no sandbox and no isolation claim for in-process Python plugins;
- no deterministic replay of arbitrary external systems;
- no Python hot reload in a running process;
- `model_calls`, `tokens`, and `estimated_cost` budget dimensions are *accounted*,
  not enforced: graphs call models, not the harness, so only wall clock, tool calls,
  and child runs are guaranteed, and the accounted limits hold only when the
  integration reports usage (see [plugin-author-guide.md](plugin-author-guide.md#budgets));
- no provider interception or generic middleware chains: scope resolution decides
  *which* provider a consumer gets, and wrapping or transforming a provider is left
  to a future release;
- no content-addressed composition DAG or persistent cross-process build cache:
  0.4 shares nodes within a process by semantic identity across live generations,
  and lifetime still follows generation reachability rather than a global cache;
- no configuration-file schema for scopes: the composition-scope primitive is stable,
  the file format is not ([configuration.md](configuration.md#composition-scopes));
- no agent loop, planner, memory framework, RAG, browser, workflow or graph DSL, MCP
  framework, or autonomous revision publisher: `AgentSpec` describes composition and
  nothing above it;
- no user or organization authorization: agent capability and tool visibility is
  composition, not IAM ([agent-composition.md](agent-composition.md));
- no child-agent delegation framework: the run model does not assume one run equals
  one agent forever, but `delegate`/`spawn_agent` are not part of this release.

## What is public API

The documented surface — everything in `chassis.__all__` and the module entry points
listed in [docs/README.md](README.md) — is covered by `tests/test_public_api.py`,
which fails if documentation references an API that no longer exists. Pre-1.0, the
minor version may break that surface; every break is recorded in
[CHANGELOG.md](https://github.com/andreolli-davide/chassis/blob/main/CHANGELOG.md) and
[migration.md](migration.md). `chassis_version` and the runtime snapshot digest
identify exactly which version produced a run. Agent composition is reached through
the `chassis.agents` namespace (`AgentSpec`, `AgentRevision`, `AgentRegistry`,
`AgentNotFound`, `AgentRetired`) rather than `chassis.__all__`.
