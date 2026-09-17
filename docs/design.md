# Design: guarantees, and what is deliberately absent

This page states what Chassis promises. It is short on purpose: every guarantee
below is enforced by tests, and the tests are the specification of record.

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

## Decisions worth knowing

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

## Deliberate absences

- no graph engine — LangGraph executes, Chassis composes;
- no competing experiment platform — LangSmith stays the evaluation home;
- no sandbox and no isolation claim for in-process Python plugins;
- no deterministic replay of arbitrary external systems;
- no Python hot reload in a running process;
- `model_calls`, `tokens`, and `estimated_cost` budget dimensions are *accounted*,
  not enforced: graphs call models, not the harness, so only wall clock, tool calls,
  and child runs are guaranteed, and the accounted limits hold only when the
  integration reports usage (see [plugin-author-guide.md](plugin-author-guide.md#budgets)).

## What is public API

The documented surface — everything in `chassis.__all__` and the module entry points
listed in [docs/README.md](README.md) — is covered by `tests/test_public_api.py`,
which fails if documentation references an API that no longer exists. Pre-1.0, the
minor version may break that surface; every break is recorded in
[CHANGELOG.md](https://github.com/andreolli-davide/chassis/blob/main/CHANGELOG.md) and
[migration.md](migration.md). `chassis_version` and the runtime snapshot digest
identify exactly which version produced a run.
