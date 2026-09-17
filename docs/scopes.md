# Scoped composition

A **composition scope** is a named node inside a candidate composition. It is the
generic primitive Chassis 0.3 adds for building runtime composition as a tree
instead of a flat list:

```text
root
├── tenant:acme
│   ├── research
│   └── finance
└── tenant:globex
    └── research
```

A scope is deliberately *not* an agent, a tenant, or a session. Those are use cases
you model **with** this primitive; naming a scope `tenant:acme` is a convention, not
a type. `AgentScope`, `TenantScope`, and `SessionScope` do not exist, and neither
does anything that executes work: a scope is composition, not execution.

Two rules define the model:

1. **A child scope is a derived composition view, not a mutable overlay.** Scopes
   exist in the control plane as desired state; resolution turns them into an
   immutable tree that rides along with the published generation.
2. **Composition decides visibility, not authorization.** A capability view narrows
   what a scope may observe. In-process Python plugins remain trusted code; the
   view is not a sandbox ([security.md](security.md)).

## 1. Root scope

Every composition has exactly one root, path `/`. A composition with no declared
scopes is still a one-scope hierarchy, so nothing needs a special case:

```python
from chassis import DATABASE, Harness

harness = Harness()
harness.install(postgres_plugin, entry_id="postgres")

async with harness:
    assert harness.current_generation.scopes.paths() == ("/",)
    assert harness.current_generation.scopes.get("/").providers  # instance ids
```

Top-level `harness.install(...)` declares an entry in the root scope, exactly as in
0.2. `harness.composition` is the desired-state tree; `generation.scopes` is the
resolved, published copy.

## 2. Child scope

```python
research = harness.composition.child("research")
research.install(search_plugin, entry_id="search")

# Equivalent: declare the scope and its entry before starting.
```

`research.path` is `/research`, derived from names — never from object identity or
memory addresses, so it is stable for diagnostics, snapshots, provenance, diffing,
and tests.

## 3. Nested scopes

```python
acme = harness.composition.child("tenant:acme")
research = acme.child("research")          # /tenant:acme/research
finance = acme.child("finance")            # /tenant:acme/finance
```

A scope may also be created by path, creating missing ancestors on request:

```python
harness.composition.child("research", parent="/tenant:acme", create_parents=True)
```

## 4. Inherited provider

A scope observes the providers of its ancestors. This is how shared infrastructure
stays shared:

```python
harness.install(postgres_plugin, entry_id="postgres")     # root
research.install(agent_plugin, entry_id="agent")          # requires database >=1,<2

async with harness:
    explanation = harness.diagnostics.explain_requirement("agent", "database")
    print(explanation.to_text())
```

```text
consumer: agent (plugin)
scope: /research
requirement: database <2,>=1

candidates:

postgres (postgres 1)
  origin: inherited (/)
  selected: only_eligible
```

The reverse never happens: a parent does not implicitly see a child's local
providers, and siblings do not see each other's.

## 5. Local provider

An entry declared in a scope is owned by that scope, and its providers are visible
there first:

```python
finance = harness.composition.child("finance")
finance.install(erp_plugin, entry_id="erp")
finance.install(ledger_plugin, entry_id="ledger")   # requires erp >=1,<2
```

Ownership is logical, and it reuses the 0.2 machinery: the *physical* owner of a
registration is still the plugin instance's `Scope`. Removing a scope, failing a
mount, or disposing an unreachable instance therefore behaves exactly as it does for
a flat composition — there is no second teardown system.

## 6. Capability narrowing

A scope may expose less than its parent:

```python
acme = harness.composition.child("tenant:acme", capabilities=[MODEL, DATABASE, TOOLS])
```

The view is a **composition visibility** control:

- it filters what the scope inherits and what it re-exposes to its descendants;
- it applies to the scope's own local providers too, so an entry for a capability
  the scope does not expose is not silently reinstated;
- it only ever narrows: a descendant's effective view is the intersection of every
  view along its path, so a child cannot widen what an ancestor hid.

```python
root provides: model, database, tools, scheduler, secrets
/tenant:acme view: model, database, tools
    → /tenant:acme/research observes model, database, tools (+ its own local providers)
    → scheduler and secrets are unresolved, and diagnostics say why
```

```python
explanation = harness.diagnostics.explain_requirement("agent", "scheduler")
assert explanation.status == "not_visible"
assert explanation.candidates[0].rejection == "capability_not_exposed"
```

Capability narrowing is not IAM, and Chassis does not claim it is.

## 7. Ambiguity

Chassis already refuses to resolve ambiguity arbitrarily, and scopes do not change
that: a valid **local** provider and a valid **inherited** provider are both
candidates. Neither silently shadows the other. The consumer stays `PENDING` until a
preference selects one — most specific first:

```python
harness.prefer_provider("database", "local-db", consumer="agent")       # one consumer
harness.prefer_provider("database", "local-db", scope="/research")      # one scope
harness.prefer_provider("database", "shared-db")                        # everywhere
```

`Version` constraints continue to apply before selection; `explain_requirement`
shows every candidate, its origin scope, whether it was visible, eligible, selected,
and why the others were rejected.

## 8. Immutable publication

Scopes are desired state until a generation is published:

```text
desired state → candidate generation → root scope → child scopes
              → resolve → validate → publish immutable generation
```

There is no path from a published generation back to a mutable scope. Adding a
scope, narrowing a view, or changing an entry is a desired-state change and becomes
visible only through the next generation:

```python
first = harness.current_generation
harness.composition.child("research")          # desired state changed
assert harness.current_generation is first     # nothing a run can see changed
await harness.reconcile()                      # publish
assert harness.current_generation.scopes.paths() == ("/", "/research")
```

A scope-affecting change publishes a new generation even when no instance changed,
because topology and resolution are observable through the generation a run
acquires.

## Worked example

```text
root
├── shared database
├── shared telemetry
├── research              (view: model, database, tools)
│   ├── search provider
│   └── model requirement
└── finance               (view: model, database, tools, erp)
    ├── ERP provider
    └── model requirement
```

```text
run A → gen_0042 → research resolves model to model-v1
control-plane change: replace model-v1 with model-v2
run B → gen_0043 → research resolves model to model-v2
run A remains on gen_0042, with model-v1 alive until its lease is released
```

The full, executed version of this example is
[examples/scoped_composition.py](https://github.com/andreolli-davide/chassis/blob/main/examples/scoped_composition.py).

## Diagnostics

```python
harness.diagnostics.scopes()                          # resolved tree, current generation
harness.diagnostics.scopes(generation_id="gen_0042")  # a specific generation
harness.diagnostics.explain_requirement("agent", "database", scope="/research")
harness.diagnostics.explain_scope("/research")
harness.diagnostics.diff_generations("gen_0042", "gen_0043")
```

`explain_scope` answers "what can this scope observe and what does it own?":
parent, children, declared/effective capability view, local and inherited providers,
owned entries and instances, its own requirements, unresolved requirements, owned
capability registrations, and tools and hooks owned by its instances. Metadata
values are redacted and configuration values are never reported.

`diff_generations` describes composition, not Python objects:

```text
gen_0042 -> gen_0043

SCOPES
  ADDED
    /tenant:acme/research

PROVIDERS
  REPLACED
    search: search-v3@1.0.0#plugin_bf06fc7f3b67 -> search-v4@1.0.0#plugin_f9b9c599acf1

REQUIREMENTS
  REWIRED
    /tenant:acme/research::researcher::database: db-a -> db-b
      reason: provided by db-b (postgres 1) inherited from / (explicit provider preference)
```

The diff is conservative: two resources are reported as reused only when entry id,
instance id, and implementation identity all match, so Chassis never claims a
semantic equivalence it cannot prove. Pass `include_unchanged=True` to also list
unchanged providers, scopes, and requirements.

## Snapshots

A snapshot identifies the scope tree, the providers per scope, the resolved
selection of every requirement, and the capability views in effect. It carries
identities and hashes, never configuration or metadata values:

```python
snapshot = harness.snapshot_for(harness.current_generation)
snapshot.to_dict()["scopes"]
# {"root": "/", "scopes": [
#   {"path": "/", "parent": None, "children": ["/research"], "capabilities": None,
#    "entries": ["postgres"], "providers": {"database": ["plugin_…"]},
#    "selections": [...]},
#   ...
# ]}
```

**Scope structure influences the snapshot digest, deliberately.** If topology or
provider resolution changes, a run observes different composition, so the digest
must change; a no-op reconcile produces the identical digest
([observability.md](observability.md#runtime-snapshots)).

## Troubleshooting

- **An unexpected provider is inherited.** `explain_requirement` names the origin
  scope of every candidate; a provider inherited from `/` is reported as
  `origin: inherited (/)`. To stop inheriting it, narrow the consuming scope's
  capability view.
- **A local and an inherited provider are `ambiguous`.** That is the designed
  behaviour: Chassis does not shadow. Declare a preference with
  `prefer_provider(..., consumer=...)` or `(..., scope=...)`.
- **A provider disappeared after capability narrowing.** The effective view is the
  intersection along the path, so an intermediate scope can hide a capability a
  descendant declares. `explain_scope(...).capabilities` shows the effective view;
  a rejected candidate reports `capability_not_exposed`.
- **An old scope's resources are still alive.** A run holds the generation that
  acquired the scope, so its instances stay until the lease is released. Check
  `generation_pressure()` and `explain_scope(path, generation_id=...)`
  ([lifecycle.md](lifecycle.md#logical-unload-is-not-physical-disposal)).

## What scopes are not

- not an execution or agent abstraction — Chassis still never runs an agent;
- not a sandbox or an authorization boundary;
- not a second lifecycle system: ownership, rollback, and disposal reuse the
  existing `Scope` and generation-reachability rules;
- not a mutable overlay on a published generation — scope-affecting changes become
  visible only through a new generation.
