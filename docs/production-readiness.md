# Production readiness review (0.9.0)

The 0.9.0 release candidate was reviewed across the same domains as the
[beta readiness review](beta-readiness.md), plus compatibility and operations.
This page is the record: what was checked, what it showed, and what remains
deliberately out of scope. 0.9.0 is the production-confidence release and the
last planned opportunity for deliberate pre-1.0 compatibility changes.

## Review outcome

| Domain | Result | Evidence |
| --- | --- | --- |
| Compatibility | pass | the 0.8.1 API baseline compares clean (`tests/test_api_compat.py`); sanitized 0.8.1 fixtures load with preserved attribution (`tests/test_format_compat.py`); fixture regeneration is byte-deterministic |
| Operational contracts | pass | planning preview is zero-mutation and apply-matching (`tests/planning/`); the signal contract is backend-independent and secret-free (`tests/telemetry/test_signal_contract.py`); OpenTelemetry adapter verified with an in-memory exporter |
| Lifecycle and concurrency | pass | 13 audited gaps closed with regressions (`tests/concurrency/test_lifecycle_hardening.py`); bounded stress and 60s+ soak return resources to baseline |
| Performance | pass with documented limits | reproducible baseline (`benchmarks/baseline-0.9.0.json`, `docs/performance.md`); structural scaling tests gate CI |
| Reference application | pass | `examples/production_reference/` executes 13 scenarios with its own assertions; deterministic transcript; CI canary |
| Documentation | pass | strict `mkdocs` build; guarantee-to-test mapping over G1–G27; links and guides synchronized |

## Roadmap items R027–R034

Every item is **complete** with direct regression coverage, documentation,
changelog entries, and migration notes (see the
[roadmap](roadmap.md#090--production-confidence)):

| Item | Delivered |
| --- | --- |
| R027 | surface classification, 0.8.1 API baseline + compatibility check, typed deprecation machinery, the compatibility policy |
| R028 | per-format versioning with explicit migration dispatch, typed rejections, 0.8.1 fixtures and provenance |
| R029 | lifecycle hardening (stop barrier, cancellation-safe shutdown, single drain deadline, disposer aggregation), resource counters, stress + soak |
| R030 | versioned planning contract and the zero-mutation `Harness.preview()` (new guarantee G26) |
| R031 | the signal contract (new guarantee G27) and the optional OpenTelemetry adapter |
| R032 | benchmark harness, recorded 0.9.0 baseline, structural scaling gates |
| R034 | the production reference application, integration test, CI canary |

## Compatibility review

- `scripts/api_compat.py check tests/compat/api-baseline-0.8.1.json` reports no
  incompatible change against the surface released as 0.8.1; 0.9.0 is purely
  additive (new modules `chassis.compat`, `chassis.planning`, the format and
  signal contracts, the OpenTelemetry adapter).
- The 0.8.1 fixtures in `tests/compat/v0.8.1/` are produced by the released
  implementation from the `v0.8.1` tag and migrate with every documented
  semantic attribution preserved. Regeneration is byte-deterministic: instance
  ids are paired from the live registry and record order is canonicalised, so
  identical semantics produce identical bytes.
- Deprecations: the typed `ChassisDeprecationWarning` and helper exist with the
  1.0 window documented; 0.9.0 deprecates nothing on purpose — the surface
  frozen for 1.0 is the reviewed 0.8.1 surface plus 0.9.0's additions.

## Operational review

- **Planning.** `Harness.preview()` computes the full plan (parse, migrate,
  validate, catalog-resolve, diff, resolve, reuse analysis, impact prediction)
  with zero mutation; preview output matches the subsequent apply in the parity
  tests. The contract document is versioned (`PLAN_FORMAT_VERSION`).
- **Telemetry.** Every span and event is declared in
  `chassis.telemetry.signals.SIGNALS` with required attributes, correlation
  fields, and bounded cardinality; the contract is enforced against the
  recording backend independent of any vendor SDK, and redaction is asserted by
  absence of secret material. The OpenTelemetry adapter transports the same
  signals (in-memory exporter verified) beside LangSmith.
- **Lifecycle.** The audit's thirteen findings are closed: `stop()` is a
  barrier for every caller and survives caller cancellation, acquisition is
  refused while stopping, `shutdown_grace_seconds` bounds the whole drain,
  plugin scopes honor the configured task timeout, a disposer raising outside
  `Exception` neither aborts the unwind nor hides sibling failures, a failed
  candidate build rolls back like a failed mount, and failed tasks and partial
  unwinds stay visible (`fully_disposed`, `stragglers`, `resource_counts()`).

## Release-candidate defect review (second round)

A second, adversarial review of the release candidate confirmed six defects —
three release-blocking — each reproduced, fixed, and pinned by a regression
test that fails before the fix and passes after (see the 0.9.0 changelog,
"Release-candidate defect review"): a teardown raising `CancelledError`
aborting shutdown and leaking instances; a stop/acquire race through the
event-loop ready queue; truncated replay documents degrading into live
sessions; a false rebuild prediction for unchanged declarative configurations;
positional signature breaks invisible to the API gate; and `@deprecated`
turning coroutine functions into regular ones. The focused suites named by the
review (lifecycle hardening, preview parity, format versions, API
compatibility) remain green and each now covers its gap.

## Independent automated audit

A read-only audit agent reviewed the milestone mid-flight. Findings and their
resolution:

| Finding | Resolution |
| --- | --- |
| 0.8.1 fixture regeneration not byte-deterministic (mount-order-dependent renaming and record order) | **fixed** (`b581cc9`): registry-paired renaming ordered by entry name, canonical record order and renumbered sequences, unpaired ids asserted; verified by regenerating twice |
| Duplicate deprecation modules (`chassis.deprecation` vs `chassis.compat`) | **not reproducible**: exactly one module exists (`src/chassis/compat.py`); no `chassis.deprecation` name appears in the source, the surface map, or any document (verified by search) |
| API baseline records unexported helper types | **verified as intended**: `ScopedCapabilities`/`ScopedTools` are documented exports of the 0.8.1 surface (`chassis.__all__`, `chassis.tools.__all__`, `tests/compat/public-api.json`); the baseline describes exactly the audited surface map and nothing else |
| Baseline extraction is file-based and nondeterministic | **not reproducible**: extraction is runtime introspection of the surface map only, and `test_description_is_deterministic` pins byte-stable output |
| Planning duplicates diagnostics | **by design**: diagnostics explain/diff are prose views; `chassis.planning` is the stable machine contract requested by R030, reusing the same resolver/identity primitives with reason codes aligned to `ReuseReason` values |
| Deprecation helpers unused | **by design**: 0.9.0 deprecates nothing; the machinery exists for the documented 1.0 window |
| Missing telemetry, stress/soak, benchmark, and example deliverables | **stale**: the audit ran mid-implementation; all four landed (`chassis/telemetry/signals.py`, `chassis/telemetry/otel.py`, `tests/concurrency/test_lifecycle_stress.py`, `scripts/soak.py`, `scripts/benchmark.py`, `examples/production_reference/`) |
| Pre-release wording in README | **accepted**; refreshed in the release-preparation commit |

## Validation matrix

| Gate | Command | Result |
| --- | --- | --- |
| Full suite, Python 3.12 | `uv run pytest` | pass (885 tests) |
| Full suite, Python 3.13 | `uv run --python 3.13 pytest` | pass |
| Branch coverage and focused floors | `pytest --cov=src/chassis --cov-branch` + `scripts/coverage_gate.py` | pass (global 91.6%, every floor met) |
| Ruff lint / format | `uv run ruff check .` / `uv run ruff format --check .` | pass |
| Pyright strict | `uv run pyright` | pass (0 errors) |
| MkDocs strict | `uv run mkdocs build --strict` | pass |
| Docs links and guarantee mapping (G1–G27) | `uv run pytest tests/test_docs_links.py` | pass |
| Wheel and sdist | `uv build` | pass |
| Clean core-wheel import | package smoke (no extras imported) | pass |
| Per-extra and combined smokes (`langgraph`, `langsmith`, `opentelemetry`) | package smoke steps | pass |
| Production reference application | `uv run python -m examples.production_reference.app` | pass (deterministic transcript) |
| Deterministic stress suite | `uv run pytest tests/concurrency` | pass |
| Soak | `uv run python scripts/soak.py --seconds 60` | pass (3,700+ cycles, all invariants) |
| Benchmarks | `uv run python scripts/benchmark.py` | pass (baseline recorded) |
| 0.8.1 compatibility | `uv run pytest tests/test_format_compat.py tests/test_api_compat.py` | pass |

## Deliberate limitations

Unchanged and still documented as truth (see
[design.md](design.md#deliberate-absences)):

- plugins execute as trusted in-process Python;
- policy is not a sandbox;
- wall-clock enforcement is cooperative unless Chassis owns the cancellable
  boundary;
- replay covers Chassis-owned boundaries, not arbitrary external state;
- graph topology or captured static inputs still require explicit
  definition-version changes;
- the control-plane resolver is superlinear at 1000 entries
  ([performance.md](performance.md#expected-capacity-and-known-scaling-limits)).

## Deferred beyond 0.9.0

- Release *publication* infrastructure (versioned documentation hosting,
  post-publication PyPI smoke tests, tag-protection and release governance,
  further GitHub Release/PyPI/SBOM/provenance workflow changes) — 1.0.0.
- Resolver optimization for large compositions — measured and documented; to be
  verified against `scripts/benchmark.py --check` and the structural tests when
  undertaken.
