# Compatibility and deprecation policy

Chassis 1.0.0b1 is the first 1.0 beta checkpoint; 0.9.1 remains the latest
stable release while it is prepared. Until 1.0.0 final, the documented surface
is technically pre-1.0, but 0.9.0 was the **last planned release with deliberate
pre-1.0 compatibility changes**. This page defines what is covered by
compatibility, what may change, how an API is retired, and how persisted data is
versioned.

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

The documented surface — the audit of every top-level export and documented
import path — is `tests/compat/public-api.json`, a machine-readable map of
module to name to stability class. It is the single source of truth: it drives
the public API tests (so an accidental addition to `__all__` or removal from a
documented module fails the suite) and the compatibility checker below.

Provisional names are listed there with `"stability": "provisional"`.
Everything not listed is internal by default. Every documented name selected
for 1.0.0b1 is stable; provisional is reserved for surface that must ship before
it can settle.

## The API baseline and the compatibility check

The API baseline is a machine-readable description of a released surface:
documented module, name, callable signature, enum values, and public dataclass
or Pydantic fields, each with its stability class. It is generated from the
released implementation — never hand-edited — by running `describe` in an
environment where that release is installed:

```bash
# in an environment with chassis-harness 0.8.1 installed
python scripts/api_compat.py describe --write tests/compat/api-baseline-0.8.1.json
```

The same tool checks the current tree against a baseline:

```bash
uv run python scripts/api_compat.py check tests/compat/api-baseline-0.8.1.json
uv run python scripts/api_compat.py check tests/compat/api-baseline-1.0.0b1.json
```

The released 0.8.1 baseline preserves the existing compatibility promise. The
1.0.0b1 baseline is generated from this beta implementation and pins the exact
candidate surface under review; it is not the permanent 1.0 baseline. That
baseline is generated from the final release candidate once the review closes.

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
and logs point at the code that needs to change. Because it subclasses the
standard `DeprecationWarning`, Python hides it outside `__main__` unless the
application opts in — for example `python -W once::chassis.compat.ChassisDeprecationWarning`
or a `filterwarnings` entry in `pyproject.toml`. The test suite runs with
warnings as errors, so an undeclared deprecation use fails CI.

### The 1.0 deprecation window

- An API deprecated in **0.9.x** may be removed **no earlier than 1.0.0**.
- 1.0.0b1 removes no stable API; any removal considered for 1.0 final must still
  satisfy this window and appear in the migration guide.
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

| Format family | Written by | Read by | Version constant |
| --- | --- | --- | --- |
| Runtime snapshot records | `RuntimeSnapshot.to_dict()` | `RuntimeSnapshot.from_dict()` | `SNAPSHOT_FORMAT_VERSION` |
| Replay recordings | `ReplaySession.to_dict()`/`save()` | `ReplaySession.from_dict()`/`load()` | `REPLAY_FORMAT_VERSION` |
| Declarative configuration | configuration documents | `load_config`/`parse_config` | schema `version: 1` |
| Planning exports | `PlanResult.to_dict()` | documented shape | `PLAN_FORMAT_VERSION` |
| Reconciliation and diagnostics exports | `ReconcileResult`, `ConfigApplyResult`, `GenerationPressureReport` `to_dict()` | documented shape | `DIAGNOSTICS_FORMAT_VERSION` |

### The 1.0 beta format candidates

`tests/compat/v1.0.0b1/` contains one deterministic candidate for every family
above: runtime snapshot, replay recording, declarative configuration, planning,
reconciliation, configuration-apply, and generation-pressure diagnostics. Its
`manifest.json` maps families to files and format versions; `SHA256SUMS` records
the exact generated bytes.

The fixtures are generated from a real harness, with volatile runtime instance
ids and clocks replaced by deterministic stand-ins and set-derived diagnostics
ordered by semantic identity:

```bash
uv run python tests/compat/v1.0.0b1/generate.py --check
```

`tests/test_format_baseline.py` runs that check in CI, proves exact round-trips
for snapshot and replay (the families with public readers), parses the
configuration fixture, and checks representative nested planning,
reconciliation, and diagnostics structures. These are review candidates; the
final release regenerates the reviewed set rather than silently relabelling
beta artifacts as the permanent 1.0 baseline.

The constants and the dispatcher live in `chassis.persistence` /
`chassis.persistence.formats`. In-process diagnostic views (`status()`,
`plugins()`, `explain()`, `metrics()` and friends) are deliberately *not*
versioned: they are read in-process from authoritative state and never written
out for later reading. A new document family that crosses a boundary must
declare a format version of its own.

Every serialized document declares its version. Readers dispatch explicitly:

- a **known older** version is migrated by a named migration step;
- the **current** version is read directly;
- a **future** version is rejected (`future_version`);
- a **malformed** version value is rejected (`malformed_version`);
- a **corrupted** payload is rejected (`corrupted`);
- a payload whose meaning cannot be recovered safely is rejected
  (`unmigratable`) — a missing migration step is a refusal, never a guess.

Rejections raise `FormatError`, whose structured context carries the
machine-readable `format`, `reason`, `found`, and `supported` values; the
meaning of an unknown version or field is never guessed.

### What migrates from 0.8.1

A payload written by 0.8.1 declares no format version (it predates versioning)
and is read as format 0, then migrated explicitly. Migration preserves semantic
attribution exactly: generation identity and sequence, agent identity and
revision, capability versions, replay boundary kind and key, redaction status,
tool and model result semantics, and every attribution field.

**What cannot be migrated:** the *semantic* scope provider map of a runtime
snapshot. 0.8.1 recorded provider *instance* ids and persisted no
instance-to-entry mapping, so a migrated snapshot reconstructs the semantic
scope tree from the recorded topology and reports empty provider maps rather
than inventing identity. `semantic_digest()` of a migrated record therefore
covers the reconstructed view; compare digests within one format version.

Sanitized fixtures produced by the released 0.8.1 implementation live in
`tests/compat/v0.8.1/`, and `tests/test_format_compat.py` asserts this contract
against them field by field.

**Support horizon.** A persisted format version stays readable — directly or by
migration — for at least **two minor releases** after the release that
superseded it. The 1.0.0b1 implementation continues to read artifacts written by
0.8.1 as described above and in [migration.md](migration.md). The final 1.0.0
release will check in the first permanent fixture for every documented format
family; the checked-in 1.0.0b1 candidates are the review material for those
schemas.

## What compatibility does not cover

- Behavior of internal modules and undocumented names.
- Exact wording of error messages, prose explanations, and text renderings
  (`to_text()`); branch on types and reason codes instead.
- Digest values across package versions: a digest covers a record that includes
  the producing `chassis_version`, so digests are comparable within one
  version. Semantic comparison across versions uses `semantic_digest()`.
- Timing, scheduling, and ordering of concurrent runs beyond the documented
  determinism of resolved composition (G12).
