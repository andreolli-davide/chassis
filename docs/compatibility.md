# Compatibility and deprecation policy

Chassis is pre-1.0. Until 1.0, a minor release may break the documented
surface — and 0.9.0 is the **last planned release with deliberate pre-1.0
compatibility changes**. This page is the policy those releases follow: what is
covered by compatibility, what may change, how an API is retired, and how
persisted data is versioned.

## Surface classes

Every documented import path and every name in `chassis.__all__` belongs to
exactly one class:

| Class | Meaning | Compatibility promise |
| --- | --- | --- |
| **Stable public API** | The documented surface: everything in `chassis.__all__` and the module entry points listed in [docs/README.md](README.md) | Pre-1.0: may break in a minor release, but only with a changelog entry and a migration note. From 1.0: breaking changes require a major release |
| **Provisional API** | New surface still settling (marked provisional in the API baseline) | May change or disappear in any release, with a changelog entry |
| **Internal API** | Everything else under `src/chassis`, including module-private names (`_`-prefixed) and undocumented modules | No promise. It may change in a patch release |
| **Persisted/serialized formats** | Data written out and read back across a process, test run, deployment, or package-version boundary | Versioned explicitly (see below); supported for at least two minor releases |

Only documented names are public. Importing an internal module may work and is
not covered: `chassis.plugins.registry` internals, helper modules, and
`_`-prefixed names are free to change. Tooling is never made public to simplify
its own implementation.

Provisional names are listed explicitly in the machine-readable API baseline
(`tests/compat/api-baseline-0.8.1.json` and successors) with
`"stability": "provisional"`. Everything not marked stable or provisional is
internal by default.

## The API baseline and the compatibility check

The API baseline is a machine-readable description of a released surface:
documented module, name, callable signature, enum values, and public dataclass
or Pydantic fields, each with its stability class. It is generated from the
released implementation — never hand-edited — by:

```bash
uv run python scripts/api_compat.py --write tests/compat/api-baseline-0.8.1.json
```

The same tool checks the current tree against a baseline:

```bash
uv run python scripts/api_compat.py tests/compat/api-baseline-0.8.1.json
```

It exits non-zero and prints a deterministic, machine-readable report when it
finds:

- a **removed** public name;
- a **moved** public name (gone from one documented module, present in another);
- an **incompatible callable signature** (removed/renamed parameter without a
  default, changed parameter kind, removed default where one existed);
- a **changed enum value** (removed member or changed member value);
- a **changed public dataclass or model field** (removed, renamed, or retyped).

Purely additive names and additive keyword parameters with defaults are
compatible changes; they still require a changelog entry and a stability
classification. `tests/test_api_compat.py` runs the same comparison in CI, so an
accidental addition, removal, or signature change fails the suite.

## Deprecations

A stable public API is never removed silently. It is deprecated first, through
the typed `ChassisDeprecationWarning` (`chassis.compat`), which always states:

1. the deprecated API;
2. its replacement (or an explicit "no replacement");
3. the version in which it became deprecated;
4. the earliest version in which it may be removed.

```python
from chassis.compat import ChassisDeprecationWarning, deprecated

@deprecated(since="0.9.0", remove_in="1.0.0", replacement="Harness.preview()")
def old_plan(self) -> object: ...
```

The warning is raised at the caller's frame, so application warnings filters
and logs point at the code that needs to change.

### The 1.0 deprecation window

- An API deprecated in **0.9.x** may be removed **no earlier than 1.0.0**.
- Anything still needed at 1.0 must be marked deprecated before 1.0 ships;
  1.0.0 removes only what this window covers.
- From **1.0.0** on, a stable public API is removed only in a major release,
  after at least one minor release carrying the deprecation warning.
- Provisional APIs may be removed in any release without a deprecation period;
  their removal is still listed in the changelog and migration notes.

0.9.0 deprecates nothing itself: the machinery and the window exist so that
1.0 can freeze a clean surface.

## Persisted formats

Data that is written out and read back later is versioned **per format
family**, with its own integer format version — never the package version,
because a format's compatibility lifecycle is independent of the code's:

| Format family | Written by | Read by | Version |
| --- | --- | --- | --- |
| Runtime snapshot records | `RuntimeSnapshot.to_dict()` | `RuntimeSnapshot.from_dict()` | `snapshot_format_version` |
| Replay recordings | `ReplaySession.to_dict()`/`save()` | `ReplaySession.from_dict()`/`load()` | `replay_format_version` |
| Declarative configuration | `HarnessConfig` documents | `load_config`/`parse_config` | schema `version: 1` |
| Planning exports | `PlanResult.to_dict()` | documented shape | `plan_format_version` |
| Reconciliation and diagnostics exports | `ReconcileResult` and report `to_dict()` | documented shape | `diagnostics_format_version` |

Every serialized document declares its version. Readers dispatch explicitly:

- a **known older** version is migrated by a named migration step;
- the **current** version is read directly;
- a **future** version is rejected (`future_version`);
- a **malformed** version value is rejected (`malformed_version`);
- a **corrupted** payload is rejected (`corrupted`);
- a payload whose meaning cannot be recovered safely is rejected
  (`unmigratable`).

Rejections raise typed Chassis errors whose structured context carries the
machine-readable reason; the meaning of an unknown version or field is never
guessed.

**Support horizon.** A persisted format version stays readable — directly or by
migration — for at least **two minor releases** after the release that
superseded it. Within 0.9.x, artifacts written by 0.8.1 are supported as
described in [migration.md](migration.md); 1.0.0 will state the first permanent
format baseline.

## What compatibility does not cover

- Behavior of internal modules and undocumented names.
- Exact wording of error messages, prose explanations, and text renderings
  (`to_text()`); branch on types and reason codes instead.
- Digest values across package versions: a digest covers a record that includes
  the producing `chassis_version`, so digests are comparable within one
  version. Semantic comparison across versions uses `semantic_digest()`.
- Timing, scheduling, and ordering of concurrent runs beyond the documented
  determinism of resolved composition (G12).
