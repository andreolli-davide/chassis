# Design: guarantees, and what is deliberately absent

This page states what Chassis promises. It is short on purpose: every guarantee
below is enforced by tests, and the tests are the specification of record.

!!! note "The 0.5.0 audit is fully remediated"
    A repository audit found reproducible gaps in the enforcement of G3, G4,
    G6, G11, G16, G23, and G24 in version 0.5.0. Version 0.5.1 closed G3, G6,
    and G11 (R001–R005), 0.6.0 closed G4, G16, G23, and G24 (R006–R012), 0.7.0
    unified the data plane (R013–R020), and 0.8.0 completed the release gates
    and the Beta readiness review (R021–R026) — see the
    [release roadmap](roadmap.md) for the auditable history and
    [beta-readiness](beta-readiness.md) for the release-candidate review.

## The proposition

> Agent runtime composition changes over time while active runs retain a coherent
> environment, dependencies react correctly, resources have explicit ownership, and
> obsolete components are disposed only when no longer reachable.

## Guarantees

| # | Guarantee | Enforced by |
| --- | --- | --- |
| G1 | Every harness-managed effect has exactly one owning scope | `core/scope.py`, `tests/core/test_scope.py::test_scope_tracks_effect_records_and_releases_them` |
| G2 | A failed plugin setup reverts every effect that setup created | `plugins/registry.py`, `tests/plugins/test_lifecycle.py::test_setup_failure_rolls_back_every_effect` |
| G3 | An active composition never contains a plugin whose selected provider is absent or incompatible | `plugins/resolver.py`, harness.py publication validation, `tests/plugins/test_publication_contract.py::test_a_registered_version_no_consumer_accepted_rolls_back` |
| G4 | A published generation is never mutated in a way visible to existing runs | `core/generation.py`, `tests/integration/test_generation_lifecycle.py::test_snapshot_observed_by_a_run_never_changes` |
| G5 | A run acquires either the old complete generation or the new one — never a partial candidate | `core/generations.py`, `tests/core/test_generation.py::test_publish_marks_the_previous_generation_draining`, `tests/integration/test_generation_lifecycle.py::test_provider_replacement_leaves_active_runs_on_their_generation` |
| G6 | A plugin scope is never physically disposed while a live generation can reach it | harness.py reclamation, `tests/concurrency/test_generation_concurrency.py::test_runs_never_observe_a_provider_that_was_disposed_under_them` |
| G7 | A plugin referenced by several live generations survives until all of them retire | `tests/integration/test_generation_lifecycle.py::test_shared_plugin_survives_until_every_generation_releases_it`, `tests/concurrency/test_generation_concurrency.py::test_disposal_happens_only_after_every_lease_is_released` |
| G8 | Normal model/tool execution never takes the control-plane lock | `harness.py`, `tests/integration/test_generation_lifecycle.py::test_data_plane_is_not_blocked_by_an_in_flight_reconcile` |
| G9 | LangGraph owns graph durability; Chassis never duplicates checkpointing | `langgraph/`, `tests/langgraph/test_langgraph.py::test_checkpointer_persists_thread_state_across_invocations` |
| G10 | In-process plugins are trusted code; policy is not sandboxing | `docs/security.md`, `tests/policy/test_policy.py::test_scoped_grant_matches_only_its_pattern`, `tests/secrets/test_secrets.py::test_secret_value_refuses_to_render_itself` |
| G11 | Secret values never enter snapshots, diagnostics, replay records, traces, or public error strings | `secrets/redaction.py`, `tests/secrets/test_secrets.py::test_redactor_scrubs_text_and_structured_payloads`, `tests/secrets/test_redaction_boundaries.py::test_replay_session_metadata_and_records_are_scrubbed_on_export`, `tests/test_evaluation.py::test_composition_metadata_never_carries_secrets` |
| G12 | Equivalent canonical desired state resolves to equivalent providers and ordering | `plugins/resolver.py`, `tests/capabilities/test_capabilities.py::test_registration_order_is_deterministic` |
| G13 | Generation liveness and lease age are read from authoritative runtime state, never derived from the bounded diagnostics history | `core/generations.py`, `diagnostics.py`, `tests/generations/test_generation_pressure.py::test_lease_age_is_authoritative_and_outlives_out_of_order_release` |
| G14 | A configured budget limit states whether Chassis enforces it or an integration must account for it | `budget/models.py`, `tests/budget/test_budget_semantics.py::test_every_dimension_declares_who_enforces_it` |
| G15 | A scope observes only its own local composition, composition inherited from its ancestors, and composition its capability view permits; sibling-local composition is not implicitly visible | `plugins/resolver.py`, `composition.py`, `tests/composition/test_scopes.py::test_sibling_scopes_do_not_see_each_others_private_providers` |
| G16 | The scope tree and every composition-affecting field of a published generation are immutable; scope-affecting changes become visible only through a new generation | `composition.py`, `core/generation.py`, `tests/composition/test_scopes.py::test_published_scope_tree_is_deeply_immutable` |
| G17 | Every resolved requirement has an authoritative provenance record (selected provider, origin scope, selection reason) and every unresolved requirement has an authoritative reason | `plugins/resolver.py`, `diagnostics.py`, `tests/composition/test_scope_diagnostics.py::test_explain_selected_provider_names_origin_and_reason` |
| G18 | A runtime node is shared across generations only when its semantic identity proves reuse cannot change observable behaviour | `core/identity.py`, `harness.py`, `tests/composition/test_semantic_identity.py::test_identical_node_reports_the_same_semantic_identity` |
| G19 | A shared runtime resource is not disposed while any live generation can reach it | `core/generations.py`, `harness.py`, `tests/composition/test_structural_sharing.py::test_a_resource_is_disposed_only_after_its_last_generation_disappears` |
| G20 | Unaffected, semantically identical nodes are eligible for reuse, and Chassis reports reuse only for a node whose reuse safety it established | `core/identity.py`, `diagnostics.py`, `tests/composition/test_reuse_diagnostics.py::test_explain_reports_physical_reuse_with_the_shared_instance` |
| G21 | A published agent revision is immutable; any composition-affecting change creates a new revision | `agents.py`, `tests/agents/test_agent_registry.py::test_a_published_revision_is_immutable` |
| G22 | A run remains associated with the agent revision selected when it started, and with the generation it acquired | `agents.py`, `runtime.py`, `tests/agents/test_invocation.py::test_a_run_keeps_its_revision_when_a_new_one_is_published` |
| G23 | Agent composition is materialized through the same scoped resolver, ownership, semantic identity, and generation publication machinery as all other composition | `agents.py`, `composition.py`, `tests/agents/test_agent_lifecycle.py::test_publication_while_an_old_revision_run_is_active` |
| G24 | Agent tool and capability visibility is composition, not authorization; it grants no user or organization authority | `docs/agent-composition.md`, `docs/security.md`, `tests/agents/test_capability_visibility.py::test_hidden_capabilities_fail_in_invoke_and_stream` |
| G25 | Every persisted Chassis format declares an explicit format version; unsupported future versions, malformed versions, corrupted payloads, and unmigratable payloads are rejected with typed machine-readable errors, never guessed | `chassis/persistence/formats.py`, `tests/test_format_versions.py::test_a_future_version_is_rejected`, `tests/test_format_compat.py::test_the_0_8_1_snapshot_migrates_without_losing_attribution` |

Every published container is deeply frozen by recursive copy-and-freeze at the
publication boundary — plugin config and manifest metadata, scopes and resolved
scopes, generation metadata, capability and tool registrations, tool policy, run
metadata, and runtime snapshots — so author-owned dicts, lists, and sets can
never alias published state. The intentional exception: executable provider and
tool objects are live runtime objects; their contracts and metadata are frozen,
not their internals.

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
[migration.md](migration.md). Since 0.9.0 the surface is classified (stable,
provisional, internal, persisted format) and checked against a machine-readable
API baseline under the
[compatibility policy](compatibility.md); 0.9.0 is the last release with
deliberate pre-1.0 compatibility changes. `chassis_version` and the runtime snapshot digest
identify exactly which version produced a run. Agent composition is reached through
the `chassis.agents` namespace (`AgentSpec`, `AgentRevision`, `AgentRegistry`,
`AgentNotFound`, `AgentRetired`) rather than `chassis.__all__`.
