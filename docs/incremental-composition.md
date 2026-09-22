# Incremental composition

0.3 could answer *what composition was resolved, and why*. 0.4 answers *what
actually changed between two generations, what must be rebuilt because of that,
and what can be safely reused*. Composition becomes incremental: a change rebuilds
only the nodes whose semantic inputs changed, and a node that did not change is the
exact same runtime instance in both generations.

This page is the mental model. The guarantees are in [design.md](design.md); the
change list is in [CHANGELOG.md](https://github.com/andreolli-davide/chassis/blob/main/CHANGELOG.md).

## One change, one rebuild

Start from a published generation with a root provider, a telemetry exporter, a
finance provider, and a research provider at version 7:

```text
gen42
root
├── postgres
├── telemetry
├── finance
└── research:v7
```

Now publish research at version 8 — same entry, same scope, same configuration,
new implementation. 0.4 reports:

```text
postgres   reused     semantic identity unchanged
telemetry  reused     semantic identity unchanged
finance    reused     semantic identity unchanged
research   rebuilt    implementation changed
```

Only `research` was replaced. `postgres`, `telemetry`, and `finance` are the same
runtime instances they were in `gen42`, and a run that still holds `gen42` keeps
observing them exactly as before. If a consumer depended on `research`, it would be
rebuilt too — see [dependency-driven rebuild](#dependency-driven-rebuild).

## Semantic identity

Every composition node — a mounted plugin instance, keyed by its entry id — has a
**semantic identity**: the set of inputs that can change what the node does.

| Input | Why it participates |
| --- | --- |
| implementation | the plugin implementation (manifest identity plus the concrete class) |
| capability contracts | `provides`, `requires`, `optional`, permissions, config version |
| configuration | the effective configuration; a credential change must force a rebuild |
| scope path | moving an entry changes which providers it may see |
| dependency bindings | the provider each requirement resolved to, as an entry *and* as a runtime instance |

Two identities compare equal only when every input above is equal. That equality is
the reuse proof: **if Chassis reuses a node, its observable behaviour cannot have
changed**.

What is deliberately *not* part of identity: generation ids, sequence numbers,
timestamps, plugin metadata, scope metadata values, and a preference that selects
the same provider it already selected. These do not change behaviour, so they never
force a rebuild.

The inputs are kept as separate fingerprints rather than one opaque hash, so a
diagnostic can say *why*:

```python
node.implementation_fingerprint
node.contract_fingerprint
node.config_fingerprint        # opaque; derived from unredacted config, never emitted
node.dependency_fingerprint
```

Fingerprints are computed from canonical hashing: mapping keys must be strings,
and non-integral floats are rounded to 12 decimal places (values differing only
beyond the 12th decimal share a fingerprint) — keep behavior-affecting
configuration well inside that precision.

`SemanticIdentity.semantic_id` is a displayable digest built from non-secret
structure only. The private fingerprints never leave the control plane: a
credential change must be *detected*, not *reported*. See
[Semantic sameness is not physical reuse](#semantic-sameness-is-not-physical-reuse).

## Dependency-driven rebuild

Impact follows real dependency bindings, not scope membership. If a consumer's
selected provider changes, the consumer is rebuilt — it captured its capability
objects during `setup`, and leaving them in place would point at a provider that is
about to be disposed. This is reported distinctly:

```text
research-provider   rebuilt    config changed
research-retriever  rewired    dependency research-provider changed
```

`rewired` means the node's own implementation, contracts, configuration, and scope
are unchanged, and only what it resolves to moved. A dependency change also
propagates transitively along the actual graph, so a provider deep in the graph
rebuilds exactly the nodes that reach it.

A **scope** change narrows impact, it does not define it: a change inside
`/tenant:acme/research` does not rebuild `/tenant:globex/research` unless there is a
real dependency edge between them. Unrelated sibling scopes are reused.

### Reason vocabulary

Every rebuild reports one or more of:

```text
config_changed
implementation_changed
dependency_changed
scope_visibility_changed
provider_selection_changed
capability_contract_changed
preference_changed
```

## Structural sharing and reachability lifetime

Reuse is *structural sharing*: two live generations reference the same runtime
instance.

```text
                    postgres
                  /        \
             gen42          gen43
```

Sharing is never shared mutability. A reused instance is never reconfigured in
place — if a change would require mutating it, the node is rebuilt instead. A
published generation therefore still never observes a composition change after
publication.

A shared resource stays alive while **any** live generation can reach it. Disposal
is unchanged from earlier releases (reuse did not add a second lifetime system):
the generation still drains its leases, and the resource is disposed only once no
live generation reaches it. Generation pressure now reports that reachability:

```python
report = harness.diagnostics.generation_pressure()
resource = next(item for item in report.resources if item.entry_id == "postgres")
resource.generations     # ("gen_0044", "gen_0043", "gen_0042")
resource.retained_by     # ("lease", "sharing")
```

`lease` means a run still holds a generation that reaches the resource; `sharing`
means more than one live generation reaches it, so no single generation's
retirement would release it.

### Stateful resources

Some runtime resources are inherently stateful — DB pools, HTTP clients, caches,
model clients, telemetry exporters. Statefulness alone does not forbid reuse. What
matters is whether the resource's **behavioural contract** is unchanged: the same
pool configuration, credential source, endpoint, and lifecycle semantics reuse
safely, because they are part of the node's configuration fingerprint. A changed
credential or endpoint changes that fingerprint and forces a rebuild.

Chassis makes no assumption about third-party mutability it cannot enforce. A
resource that is reconfigured in place is not reused: a change is a new instance,
and the old one is shared only until no live generation reaches it. If a plugin
mutates shared state outside the inputs Chassis can see, that is the plugin's
contract to keep, not something structural sharing will paper over.

Reuse safety is inferred from semantic identity; 0.4 adds no opt-in or opt-out
policy, because the conservative default is already the safe one: anything Chassis
cannot prove safe is rebuilt.

## Semantic sameness is not physical reuse

These are two different facts, and 0.4 keeps both explicit:

- **semantically unchanged** — the node's identity is equal; reuse *would* be safe.
- **physically reused** — the exact same lifecycle-managed instance was retained.

They normally coincide. They can differ: replacing an entry with a semantically
identical implementation forces a new revision, hence a new instance, without
changing behaviour. That is reported as `UNCHANGED` (semantically identical) rather
than `REUSED` (which is only ever claimed when the instance really was retained).
The runtime snapshot makes the same distinction:

```python
snapshot.semantic_digest()   # composition identity; equal across separately built equivalents
snapshot.physical_digest()   # the runtime instance ids
snapshot.digest()            # the whole record
```

## Asking why

```python
old, new = gen42.generation_id, gen43.generation_id

harness.diagnostics.diff_generations(old, new)          # structural diff + a NODES section
harness.diagnostics.analyze_impact(old, new)            # ImpactAnalysis over every node
harness.diagnostics.explain_reuse(old, new, "research") # ReuseExplanation for one node
```

A `ReuseExplanation` carries the decision, the reasons, the changed inputs, the
dependency that changed, and the shared instance id:

```python
explained = harness.diagnostics.explain_reuse(old, new, "research")
explained.decision            # "rebuilt"
explained.reasons             # ("implementation_changed",)
explained.changed_inputs      # ("implementation",)
explained.shared_instance_id  # None
explained.to_dict()           # structured, JSON-compatible
```

`ReconcileResult.impact` carries the same analysis for the reconciliation that just
ran, and `harness.diagnostics.analyze_impact` recomputes it from any two published
generations.

## Agent revisions

An [agent revision](agent-composition.md) is a composition change like any other, so
the same rules apply: a revision that leaves a contribution unchanged does not
reinstall its entry, and the mounted instance is carried across the revision change.
A revision that adds a contribution mounts only that node; a revision that changes a
scope's capability or tool view narrows or widens visibility and rebuilds exactly the
consumers whose resolved bindings moved. Unrelated agent scopes are reused, and a
shared provider is retained.

## Rollback

Candidate construction is still transactional. New nodes are mounted before
anything is published; reused nodes are shared, never re-created. If building a new
node fails:

- every candidate-owned mount is rolled back;
- reused nodes from older generations are untouched;
- the current generation remains current;
- no partial candidate becomes visible.

## Limitations

- Reuse is conservative. Anything Chassis cannot prove safe is rebuilt: a changed
  revision rebuilds even when the new node is semantically identical.
- The semantic digest reflects *redacted* configuration, so a secret-only change is
  invisible in `snapshot.semantic_digest()`. It still forces a rebuild, because the
  private identity fingerprint is computed from the effective configuration.
- There is no content-addressed build cache, no cross-process sharing, and no
  persistent composition graph. Sharing is per process and follows generation
  reachability.
- Identity is a proof of *observable* equivalence for inputs Chassis can see.
  A plugin that reads a file, clock, or remote endpoint at setup time can still
  behave differently after reuse; Chassis does not virtualize external systems.
