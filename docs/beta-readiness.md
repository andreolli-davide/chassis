# Beta readiness review

The 0.8.0 release candidate was re-audited against the same five domains as the
0.5.0 audit. This is the record of that review: what was checked, what it
showed, and what remains deliberately out of scope.

## Audit outcome

| Domain | Result | Evidence |
| --- | --- | --- |
| Architectural | pass | 730 tests; guarantees G1–G24 each map to an existing test node (`tests/test_docs_links.py`); no Critical or High roadmap item open |
| Security | pass | fail-closed policy/secret resolution (R002), one redaction boundary with an adversarial leak matrix (R003), `pip-audit` clean with a documented triage process |
| Concurrency | pass | real-task regressions for lease accounting, hot replacement, shutdown, and cancellation (R001, R005, R006, R014) |
| Packaging | pass | wheel and sdist smoke-tested in clean environments; core, `langgraph`, `langsmith`, and combined extras exercised independently; SBOM and PyPI attestations ship with releases |
| Documentation | pass | strict `mkdocs` build; the guarantee-to-test mapping test; docs synchronized with their regression tests (R023) |

## Roadmap items R001–R026

Every item is **complete** — none deferred, none replaced. Critical items
(R002, R003) and High items (R001, R004, R006–R010) all landed with their
regression tests, documentation, changelog entries, and migration notes. The
release gates enforce branch coverage at the 0.5.0 baseline plus focused floors,
minimum/latest dependency compatibility, and lint, typing, and docs builds
(R024, R025).

## Public API review

The top-level surface is pinned in `tests/test_public_api.py`, which now also
guards against accidental exports: anything outside the documented list fails
the suite. One accidental export was found and removed (`DependencyResolver`;
`chassis.plugins` remains the import path for the resolver engine).

## Deliberate limitations

These are documented scope decisions, not gaps (see
[design.md](design.md#deliberate-absences)):

- the policy engine governs the harness, not sandboxed code;
- replay records Chassis-owned boundaries and does not virtualize external
  systems;
- wall-clock budget enforcement is cooperative at harness boundaries;
- canonical hashing treats values differing only beyond the 12th decimal place
  as equal, and rejects `Decimal`-like and non-string mapping keys.

## Migration

All pre-1.0 breaking changes since 0.5.0 are documented in
[migration.md](migration.md), one section per minor release.
