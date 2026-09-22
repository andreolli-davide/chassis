# Planning: the machine-readable dry-run contract

`Harness.preview()` answers "what would an apply do, and why" as stable
structured data — for CI gates, deployment tooling, and operator review —
computed with **zero mutation** (guarantee G26). It runs the full pipeline a
subsequent `apply_config()` + `reconcile()` would: parsing, migration,
validation, catalog resolution, desired-state diffing, dependency resolution,
semantic-identity reuse analysis, and generation-impact prediction.

```python
plan = harness.preview()                      # the next reconcile
plan = harness.preview(open("harness.yaml"))  # a configuration change

plan.would_publish          # would this apply publish a new generation?
plan.to_dict()              # deterministic, versioned JSON document
plan.action_for("memory")   # one entry's planned action
```

Preview performs **no** mutation: no revision bump, no dirty flag, no mounted
plugin, no published generation, and no setup or cleanup effect. Plugin objects
are constructed exactly as `install()` would (so instance-configured manifests
work) and discarded unmounted.

## Actions

One action per affected entry, ordered by entry id, with a final publication
action last. The vocabulary is closed (`chassis.planning.ActionKind`):

| Action | Meaning |
| --- | --- |
| `add` | the entry joins the composition with a new instance |
| `remove` | the entry leaves the composition |
| `replace` | desired state replaces the entry (implementation or configuration changed) |
| `reuse` | the exact mounted instance is carried into the next generation |
| `rebuild` | the entry stays, but a semantic input changed so it must be rebuilt |
| `reject` | the entry cannot join: unsatisfied, ambiguous, pending, or cyclic |
| `publish` | the apply would publish a new generation |
| `no-op` | the composition is identical; nothing would be published |

Every action carries **stable reason codes** (`ReasonCode`) — the values are the
contract, never rendered prose — plus, where applicable: the affected entry and
scope, configuration **keys** (never values), the dependency or capability
cause (`cause_capability` / `cause_provider`), `expected_reuse` with the
expected `instance_id`, `generation_impact`, machine-readable
`validation_failures` (`code` + `detail`), and `ambiguities` (candidates and
the applicable preference).

```json
{
  "action": "rebuild",
  "reasons": ["dependency_changed"],
  "entry_id": "memory",
  "plugin": "memory@2.1.0",
  "scope": "/",
  "config_keys": [],
  "cause_capability": "database",
  "cause_provider": "orders-db",
  "expected_reuse": false,
  "instance_id": null,
  "generation_impact": "new_generation",
  "validation_failures": [],
  "ambiguities": []
}
```

## The document

`PlanResult.to_dict()` is deterministic and declares its own format version
(`PLAN_FORMAT_VERSION`, never the package version — see
[compatibility.md](compatibility.md#persisted-formats)):

```json
{
  "format_version": 1,
  "chassis_version": "0.9.0",
  "would_publish": true,
  "current_generation_id": "gen_0007",
  "pending": ["memory"],
  "cycles": [],
  "preferences": {"memory:database": "orders-db"},
  "actions": ["…"]
}
```

`preferences` are the effective provider preferences the plan resolved with —
selector keys to provider entry ids, never configuration material. Sensitive
configuration appears only as `config_keys`; a preview output contains no
configuration values at all.

## What preview does and does not decide

Preview reports plan-level problems as `reject` actions with structured
failures: unsatisfied or ambiguous requirements (`no_provider`,
`version_mismatch`, `not_visible`, `self_reference`, `ambiguous`),
provider-pending dependencies, and dependency cycles. Configuration-level
errors (an unknown plugin, a malformed preference) raise exactly what
`apply_config()` would raise. Publication-time contract validation — checking
that mounted instances actually register what their manifests promise — needs
mounted registrations and runs only during a real `reconcile()`.

Parity with apply is a tested contract: the previewed actions are compared with
what `apply_config()` + `reconcile()` actually mounted, reused, and disposed,
and with the generation-impact analysis it reports
(`tests/planning/test_preview_parity.py`).

There is deliberately no CLI: the contract is a library API. Tooling should
call `Harness.preview()` and read `to_dict()`; a thin wrapper script would add
no semantics of its own.
