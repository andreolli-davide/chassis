# Release roadmap

This roadmap turns the 0.5.0 repository audit into release work. It is an
ordered correctness plan, not a calendar commitment: a release ships when its
exit criteria are met, and unfinished work moves forward explicitly rather than
being silently dropped.

The roadmap covers known defects, test gaps, documentation corrections, and
release-engineering work. New features are intentionally secondary until the
security and runtime-lifetime invariants are restored.

## How to read this roadmap

- **Critical** means a security boundary can fail open or secret material can
  leave its intended boundary.
- **High** means a documented composition, generation, or lifecycle guarantee
  can be violated.
- **Medium** means a supported workflow is inconsistent, non-atomic, or fails
  late with a misleading result.
- **Low** means API hardening, diagnostics quality, or release hygiene.
- An item is complete only when the implementation, regression tests, public
  documentation, changelog, and migration notes (when needed) land together.
- G1–G24 remain the intended contracts. The audit gaps affecting G3, G4, G6,
  G11, G16, G23, and G24 are tracked below and must not be treated as verified
  properties of 0.5.0.

## Release sequence

| Release | Theme | Required outcome |
| --- | --- | --- |
| **0.5.1** | Security and lifetime hotfix | Fail closed, prevent secret leakage, and make generation drain trustworthy |
| **0.6.0** | Transactional composition | Make publication, replacement, visibility, and immutable state correct across live generations |
| **0.7.0** | Data-plane consistency | Give invoke, stream, replay, hooks, budgets, and telemetry one coherent execution contract |
| **0.8.0** | Beta-readiness hardening | Remove API sharp edges and add the compatibility, coverage, packaging, and supply-chain gates needed for Beta |

No release after 0.5.0 should introduce new runtime surface before the 0.5.1
exit criteria are satisfied. Work may be developed in parallel, but the releases
must preserve this order because later milestones depend on the corrected lifetime
and security foundations.

## 0.5.1 — security and lifetime hotfix

This patch release addresses defects that can invalidate a security decision,
dispose live state, or publish a composition that was never actually satisfied.
The fixes should avoid intentional public API breaks.

### R001 — make generation lease accounting authoritative

**Severity:** High. **Affected guarantees:** G6, G7, G13, G19, G22.

- Clear the idle event on the `0 -> 1` lease transition.
- Decrement the lease count only after validating and removing the exact lease id.
- Define duplicate release as an idempotent no-op or a typed error; never alter
  accounting for an unknown lease.
- Reject normal retirement while leases remain. Keep any force-retirement path
  internal and explicit for terminal shutdown only.
- Validate generation history limits as non-negative.
- Add concurrent regressions for reacquisition after idle, unknown and duplicate
  releases, shutdown during a live second lease, and shared-instance reachability.

### R002 — make policy resolution fail closed

**Severity:** Critical. **Affected guarantees:** security policy contract.

- Resolve policy and secret providers as explicit system requirements rather than
  through a helper that treats ambiguity like absence.
- Use the configured default only when no provider exists.
- Reject more than one eligible provider unless an explicit preference selects one.
- Ensure resolution and provider failures deny the tool call rather than falling
  back to `AllowAllPolicy`.
- Add tests with two deny policies, ambiguous scoped policies, provider failure,
  and explicit preference.

### R003 — establish one end-to-end redaction boundary

**Severity:** Critical. **Affected guarantee:** G11.

- Create one harness-owned `SecretRedactor` and inject it into replay, diagnostics,
  tool execution, agent execution, LangGraph, recording telemetry, and LangSmith.
- Redact recursively in requests, responses, metadata, configuration material,
  span attributes, span updates, events, cleanup reports, and public error strings.
- Apply sensitive-key redaction to nested mappings and sequences on both request
  and response paths.
- Sanitize policy-denial reasons and agent/runtime exceptions before they cross a
  public, hook, diagnostic, replay, or telemetry boundary. Preserve the original
  exception only as an internal cause.
- Define and test the treatment of secret values shorter than four characters;
  the target contract is that they are protected too.
- Redact replay-session metadata and tool/capability metadata.
- Add an adversarial matrix covering nested dicts/lists, short values, exception
  messages, cleanup failures, policy reasons, streamed events, LangSmith attribute
  updates, and externally supplied replay sessions.

### R004 — validate actual capability registrations before publication

**Severity:** High. **Affected guarantees:** G3, G5, G17.

- Compare every mounted plugin's effective registrations with the contracts its
  manifest claims to provide.
- Recompute or validate the resolution fixpoint after mount and before candidate
  publication.
- Roll back the entire candidate when an active consumer lacks its selected
  provider or the registered version is incompatible.
- Produce a structured diagnostic naming the provider, promised contract, actual
  registrations, affected consumers, and rollback result.
- Add a regression where a provider declares a capability but registers none,
  including the first generation where no prior live registration exists.

### R005 — make failed scope entry and failed plugin cleanup reversible

**Severity:** Medium. **Affected guarantees:** G1, G2.

- Register context-manager effects only after successful entry, or remove their
  records when `__enter__`/`__aenter__` fails.
- Reclaim failed plugin instances once they are no longer desired or reachable.
- Aggregate setup and rollback failures without hiding cleanup errors.
- Report cancellation-resistant tasks that remain alive after the shutdown timeout;
  do not present their scope as fully disposed.
- Add tests for failed sync/async entry, failed setup plus failed cleanup, orphaned
  `FAILED` instances, and cancellation-resistant tasks.

### 0.5.1 exit criteria

- Every R001–R005 regression fails on 0.5.0 and passes on the release branch.
- Security documentation no longer contains an unqualified statement that is false
  for an exercised public path.
- Full Python 3.12 and 3.13 suites, strict typing, lint, documentation, wheel smoke,
  and sdist smoke all pass.
- A focused security review confirms no raw secret is present in recorded values,
  public exceptions, diagnostics, or telemetry fixtures.

## 0.6.0 — transactional composition and generation-safe registries

This release may make pre-1.0 API changes. Its purpose is to make every candidate
generation self-contained and allow old and new registrations to coexist until
reachability permits disposal.

### R006 — key tool and agent registrations by identity

**Severity:** High. **Affected guarantees:** G4, G6, G18–G23.

- Give every tool and agent-runtime registration an immutable registration id.
- Maintain name indexes separately from the registration store.
- Allow old and new generations to reference same-named registrations concurrently.
- Build generation snapshots from registration ids owned by the generation's
  selected instances.
- Make cleanup identity-checked so closing an old scope cannot remove its successor.
- Preserve explicit duplicate-name errors within one resolved generation.
- Test hot replacement of same-named tools and runtimes while an old run remains
  leased, including cleanup in both possible release orders.

### R007 — bind the exact selected capability contract

**Severity:** High. **Affected guarantees:** G3, G12, G17, G18.

- Carry the selected contract key, version, provider identity, and eventually the
  concrete registration id through `RequirementResolution`.
- Filter materialized registrations with the original requirement predicate; never
  select only by capability name and provider instance.
- Remove UUID-dependent choice among multiple registrations.
- Infer an API major from a version specifier only when the accepted range proves a
  single major; otherwise require an explicit contract generation or keep the
  requirement generation-neutral.
- Key application provisions by full `CapabilityKey`, not capability name.
- Test multi-contract providers, open ranges such as `>1.9,<3`, exclusions,
  disjoint ranges, simultaneous v1/v2 provisions, and deterministic snapshots.

### R008 — enforce AgentSpec composition visibility at runtime

**Severity:** High. **Affected guarantees:** G15, G23, G24.

- Build a capability snapshot filtered by the acquired `ResolvedScope.visible`
  registrations.
- Use the scoped snapshot for `HarnessRunContext.require_capability()` and for
  graph build-time capability versions.
- Keep the documented distinction between composition visibility and security
  authorization; this is isolation of composition, not a sandbox.
- Reject an unknown scope path rather than returning an unexplained empty tool view.
- Test hidden, inherited, narrowed, sibling-local, and versioned capabilities in
  both invoke and stream paths.

### R009 — make published state deeply immutable

**Severity:** High. **Affected guarantees:** G4, G16, G18, G21.

- Apply recursive copy-and-freeze at every publication boundary: plugin config and
  manifest metadata, scopes, resolved scopes, generation metadata, capability and
  tool registrations, tool policy, run metadata, and runtime snapshots.
- Ensure author-owned dicts, lists, sets, and Pydantic values cannot alias published
  state.
- Ensure semantic fingerprints are computed from exactly the frozen state that a
  generation exposes.
- Document the intentional exception that executable provider/tool objects are live
  runtime objects; freeze their contracts and metadata, not arbitrary internals.
- Add nested mutation tests for every published container type.

### R010 — make AgentSpec materialization transactional and owner-safe

**Severity:** High. **Affected guarantees:** G5, G16, G21, G23.

- Refuse implicit takeover of a pre-existing user scope, or materialize an
  agent-owned overlay without rewriting user state.
- Stage capability views, tool views, requirements, metadata, and plugin
  contributions before mutating desired state.
- On failure, restore the exact previous scope and registry state.
- On withdrawal, remove only state owned by that revision.
- Verify reserved agent metadata cannot collide with user metadata.
- Add tests for existing scopes, mid-materialization catalog errors, replacement
  failure, withdrawal, and concurrent old/new revisions.

### R011 — make declarative configuration atomic

**Severity:** Medium. **Affected guarantees:** G5, G12, G16.

- Parse, validate, catalog-resolve, and diff the complete configuration before
  mutating desired state.
- Commit entries, preferences, and stored config as one transaction.
- Replace config-owned preferences instead of retaining omitted values.
- Reject unsupported schema versions; introduce an explicit migration dispatcher
  before accepting a second schema version.
- Preserve runtime/programmatic preferences separately if both sources are meant to
  coexist, and document precedence.
- Add rollback tests for unknown plugins, invalid preferences, partial removals, and
  repeated idempotent application.

### R012 — harden composition-tree ownership and naming

**Severity:** Medium/Low. **Affected guarantees:** G15, G16.

- Reject a parent scope belonging to another `CompositionTree`.
- Require canonical absolute paths consistently across tree and AgentSpec APIs.
- Normalize or reject whitespace-only capability, permission, catalog, entry, and
  agent names.
- Validate non-negative config and manifest schema versions.
- Add cross-tree, relative-path, and invalid-name tests.

### 0.6.0 exit criteria

- Same-named old/new tools and agent runtimes coexist safely under concurrent leases.
- No author-owned mutable container can change a published generation or revision.
- A candidate cannot publish unless every exact capability binding is satisfiable.
- Agent and declarative-config failures leave desired and published state unchanged.
- Migration notes document every changed registry, scope, config, or validation API.
- Guarantees G3, G4, G5, G6, G12, G15–G24 have direct regression coverage for the
  scenarios above.

## 0.7.0 — data-plane consistency

This release unifies execution semantics so that invocation mode, replay mode, and
telemetry backend do not change policy, budget, hooks, or attribution.

### R013 — reconcile before agent lookup

**Severity:** Medium.

- Call `ensure_ready()` before resolving an agent runtime or logical revision.
- Acquire the generation before selecting generation-owned registrations.
- Teach evaluation targets to resolve logical AgentSpec names as well as raw runtime
  names.
- Test agents installed or replaced after harness start and evaluation of logical
  agents.

### R014 — unify invoke and stream execution pipelines

**Severity:** Medium. **Affected guarantee:** G22.

- Share one internal run lifecycle for readiness, generation acquisition, agent
  resolution, budget scope, hooks, telemetry, snapshot attribution, and cleanup.
- Emit the same `agent.run` span and snapshot digest for invoke and stream.
- Stamp logical agent, revision, generation, run, and thread attribution on every
  streamed event rather than trusting a custom runtime to do it.
- Keep the parent budget active through before/after hooks and child-agent calls.
- Add parity tests that compare invoke and stream for attribution, hook failures,
  nested budgets, cancellation, and telemetry.

### R015 — make replay keys and results semantically complete

**Severity:** Medium.

- Include normalized stop sequences and invocation kwargs such as temperature,
  tools, structured-output/response format, and provider options in model keys.
- Preserve stable `llm_output` and generation metadata in recorded results.
- Detect or reject non-canonical values instead of silently omitting them.
- Make `ReplaySession.has()` cursor-aware or introduce `has_remaining()`/`peek()`.
- Add collision tests for requests differing by one semantic option and exhaustion
  tests for repeated keys.

### R016 — make replayed tool calls follow the live boundary

**Severity:** Medium.

- Execute authorization, approval, budget, before/after hooks, and telemetry in the
  same order for live and replayed calls.
- Reuse only the recorded semantic result; stamp current run, generation, call id,
  and timing attribution.
- Document which timing and provider metadata are historical and which are current.
- Test denial after recording, after-hook observation, span parity, and replay in a
  different generation.

### R017 — settle hook semantics and surface failures

**Severity:** Medium.

- Choose and document whether `TRANSFORM` replaces a payload or patches it. Because
  the current documentation promises replacement, prefer implementing replacement;
  record any compatibility impact in migration notes.
- Deep-freeze hook payloads if they remain documented as immutable.
- Emit `RECORD` failures from tool and agent data-plane hooks to structured telemetry
  and diagnostics without failing the operation.
- Test key removal, nested mutation attempts, multiple transforms, and recorded
  failures in control and data planes.

### R018 — validate budgets and tool contracts at construction

**Severity:** Medium.

- Reject negative and non-finite limits and consumption amounts.
- Require integer amounts for count dimensions; never truncate fractions silently.
- Normalize `ToolPolicy` sequences, deep-freeze metadata, and require a positive
  finite timeout when present.
- Reject an obviously synchronous `ainvoke` implementation or normalize and verify
  awaitability at the boundary with a typed error.
- Document that wall-clock enforcement is cooperative unless the harness owns a
  cancellable boundary.
- Add property/boundary tests for zero, negative, infinity, NaN, fractional counts,
  mutable policy inputs, and sync `ainvoke`.

### R019 — isolate telemetry failures and unify adapter wiring

**Severity:** Medium.

- Isolate every `TeeTelemetry` backend during span enter, updates, events, errors,
  and exit so one backend cannot suppress another or break runtime correctness.
- Define a safe telemetry wrapper for arbitrary custom backends.
- Bind Harness telemetry and redaction into registered LangGraph agents unless an
  explicit, documented override is requested.
- Preserve visibility of telemetry-backend failures through a safe fallback logger
  or diagnostic counter.
- Test failures at every span phase and confirm subsequent backends still receive
  complete events.

### R020 — correct runtime snapshots and graph cache validation

**Severity:** Medium.

- Obtain runtime identity from the selected `AgentRuntime`; do not default custom
  runtimes to `langgraph`.
- Validate `GraphCache.max_entries` and define whether zero disables caching.
- Document and test that topology or captured static inputs require an
  `AgentDefinition.version` change.
- Prefer graph builders that route runtime-varying tools through the harness
  boundary; add a diagnostic or guide warning for captured static implementations.

### 0.7.0 exit criteria

- Invoke, stream, and replay share policy, hook, budget, attribution, and telemetry
  behavior except where a documented replay limitation necessarily differs.
- Telemetry backend failures cannot fail a tool or agent operation.
- Model replay cannot collide across any documented semantic invocation field.
- All validation errors occur at construction/registration or the earliest safe
  boundary and use typed exceptions.

## 0.8.0 — Beta-readiness hardening

This milestone closes lower-risk audit findings and raises project gates. Reaching
its exit criteria is the prerequisite for changing the package classifier from
Alpha to Beta; the classifier change itself should happen only in the release that
actually meets every gate.

### R021 — harden persistence canonicalization

**Severity:** Low.

- Require string mapping keys or detect collisions after normalization.
- Document or remove 12-digit float rounding for behavior-affecting configuration.
- Add deterministic tests for mixed key types, Unicode, signed zero, non-finite
  floats, decimal-like values, and nested order variation.

### R022 — remove API ambiguity and silent fallbacks

**Severity:** Low.

- Reject convenience arguments when a complete `AgentRequest` is supplied instead
  of silently ignoring `thread_id`, `resume`, `checkpoint_id`, or metadata.
- Extend `AgentResult.text` to documented mapping messages or explicitly narrow its
  supported input contract.
- Distinguish missing scopes from valid empty scopes in runtime APIs.
- Make replay exhaustion distinguishable from a missing key.
- Review all public defaults for silent permissive behavior and replace ambiguous
  cases with typed errors.

### R023 — align diagnostics and documentation with implementation

**Severity:** Low/Medium.

- Replace raw exception strings in status and cleanup reports with a structured,
  redacted public error representation.
- Correct the resolver documentation about active-provider preference.
- Keep the design guarantees, security guide, replay guide, observability tables,
  and hook documentation synchronized with their regression tests.
- Add a documentation test mapping every guarantee to at least one existing test
  node, rather than checking links alone.

### R024 — establish coverage and compatibility gates

**Severity:** Release engineering.

- Add branch coverage for `src/chassis` to CI, starting no lower than the measured
  0.5.0 baseline of 91% unless an intentional, reviewed exception is documented.
- Add a minimum-supported direct-dependency job and a latest-compatible job.
- Keep the lockfile job as the reproducible development baseline.
- Add focused coverage expectations for lifecycle, resolver, registry, security,
  replay, and telemetry rather than relying only on one global percentage.

### R025 — expand package and dependency verification

**Severity:** Release engineering/security hygiene.

- Install and smoke-test both wheel and sdist in clean environments.
- Smoke-test core, `langgraph`, `langsmith`, and combined extras independently.
- Add a dependency vulnerability scan such as `pip-audit` or OSV scanning with a
  documented triage/exception process.
- Review lower-bound-only dependencies through compatibility CI; add upper bounds
  only where an upstream compatibility break makes them necessary.
- Produce an SBOM and attest/provide build provenance for published artifacts if the
  selected PyPI/GitHub workflow supports it reliably.

### R026 — complete Beta readiness review

**Severity:** Release governance.

- Re-run the architectural, security, concurrency, packaging, and documentation
  audit against the release candidate.
- Confirm every item R001–R025 is complete, explicitly deferred with rationale, or
  replaced by a stronger solution. Critical and High items cannot be deferred.
- Review the public API and remove accidental exports before declaring Beta.
- Publish migration instructions covering all pre-1.0 breaking changes since 0.5.0.
- Change the classifier to Beta only after the audit and release gates pass.

### 0.8.0 exit criteria

- No open Critical or High roadmap item.
- No known contradiction between a documented guarantee and a reproducible public
  behavior.
- Branch coverage and focused invariant suites are enforced in CI.
- Minimum/latest dependency, wheel, sdist, core, and optional-extra jobs pass.
- Dependency scanning has no untriaged applicable vulnerability.
- The release candidate passes a fresh audit and has complete migration notes.

## Traceability to the 0.5.0 audit

The table ensures every audit finding has a destination. A row may map to more than
one item where a cross-cutting fix is required.

| Audit finding | Roadmap item | Status |
| --- | --- | --- |
| Idle event remains set after lease reacquisition; unknown/double release corrupts accounting | R001 | complete (0.5.1) |
| `GenerationManager.retire()` and history-limit guard gaps | R001 | complete (0.5.1) |
| Multiple policy providers fall back to allow-all | R002 | complete (0.5.1) |
| Harness/replay/LangGraph/LangSmith use disconnected redactors | R003, R019 | complete (0.7.0) |
| Nested request/response, metadata, error, cleanup, policy, and stream secret leaks | R003, R023 | partial (R003 complete in 0.7.0) |
| Secret values shorter than four characters are not redacted | R003 | complete (0.5.1) |
| First generation can publish a consumer whose provider did not register its declared capability | R004 | complete (0.5.1) |
| Failed context entry leaves an effect record | R005 | complete (0.5.1) |
| Failed plugin instances and cleanup failures remain hidden or resident | R005 | complete (0.5.1) |
| Cancellation-resistant tasks can outlive a closed scope | R005 | complete (0.5.1) |
| Same-named tool/agent registrations block hot replacement or remove their successor | R006 | complete (0.6.0) |
| Capability binding ignores the exact selected version/contract | R007 | complete (0.6.0) |
| Multi-major specifiers are pinned to an incorrect lower-bound major | R007 | complete (0.6.0) |
| Application provisions are keyed only by capability name | R007 | complete (0.6.0) |
| Agent capability view is not enforced in `HarnessRunContext` | R008 | complete (0.6.0) |
| Unknown runtime scope silently becomes an empty tool view | R008, R022 | partial (R008 complete in 0.7.0) |
| Config, scope, registration, policy, run, and snapshot state are shallow-frozen | R009 | complete (0.6.0) |
| AgentSpec overwrites user scopes and does not roll back partial materialization | R010 | complete (0.6.0) |
| `apply_config` is non-atomic and retains stale preferences | R011 | complete (0.6.0) |
| Unsupported configuration schema versions are accepted | R011, R012 | complete (0.6.0) |
| Composition trees accept foreign parents and inconsistent paths/names | R012 | complete (0.6.0) |
| Agent lookup happens before readiness reconciliation | R013 | complete (0.7.0) |
| Evaluation cannot target logical AgentSpec names | R013 | complete (0.7.0) |
| Stream lacks invoke-equivalent spans, attribution, hooks, and budget scope | R014 | complete (0.7.0) |
| Model replay omits semantic kwargs and result metadata | R015 | complete (0.7.0) |
| Replay presence ignores cursor exhaustion | R015, R022 | partial (R015 complete in 0.7.0) |
| Replayed tools skip live-path spans/after-hooks and retain historical ids | R016 | complete (0.7.0) |
| Hook transform semantics, shallow payloads, and recorded failures are inconsistent | R017 | complete (0.7.0) |
| Negative/fractional/non-finite budgets are accepted | R018 | complete (0.7.0) |
| Tool protocol and ToolPolicy validation is too weak | R018 | complete (0.7.0) |
| Tee/custom telemetry failures can break runtime or suppress later backends | R019 | complete (0.7.0) |
| Independently built LangGraph agents fragment telemetry/redaction | R003, R019 | complete (0.7.0) |
| Custom runtimes are labelled `langgraph`; graph-cache capacity is unchecked | R020 | complete (0.7.0) |
| Static graph inputs can retain obsolete implementations | R020 | complete (0.7.0) |
| Canonical hashing can collide after stringifying mapping keys; float rounding is implicit | R021 | open |
| Mixed AgentRequest arguments are silently ignored | R022 | open |
| `AgentResult.text` omits mapping-shaped messages | R022 | open |
| Raw diagnostic errors and stale resolver/hook/observability claims | R003, R017, R023 | partial (R003, R017 complete in 0.7.0) |
| No coverage threshold or focused invariant coverage | R024 | open |
| No minimum/latest dependency compatibility matrix | R024, R025 | open |
| No sdist or clean LangSmith-extra smoke test | R025 | open |
| No dependency vulnerability scan, SBOM, or provenance gate | R025 | open |

## Definition of done for every roadmap item

1. The smallest public behavior that demonstrates the defect has a regression test.
2. Concurrency and lifetime claims use real tasks and event barriers, not a
   sequential simulation.
3. Security regressions assert absence of the secret material from every exported
   representation, not merely presence of `<redacted>` in one output.
4. The implementation has no compatibility shim unless the migration policy
   explicitly requires one.
5. `CHANGELOG.md`, affected guides, design guarantees, and migration notes agree
   with the implemented behavior.
6. Ruff, formatting, strict Pyright, the full test suite, strict MkDocs, wheel/sdist
   verification, and the release-specific gates pass.
7. The traceability row is marked complete in the release pull request; it is not
   removed from this document, so the repair history remains auditable.

