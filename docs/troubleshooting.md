# Troubleshooting

Symptom first. Every entry names the real error or the real diagnostics output, and
the API that answers it: diagnostics are generated from authoritative state, so you
never have to reverse-engineer behaviour from logs.

Reach for these before anything else:

```python
harness.diagnostics.status()            # harness state, plugin/tool/hook/agent counts, composition
harness.diagnostics.plugins()           # per-entry state, health, effects, resolution
harness.diagnostics.explain("memory")   # why one plugin is active, pending, or excluded
harness.diagnostics.generations()       # leases, state, instances per generation
harness.diagnostics.generation_pressure()  # who is still live, and why
harness.diagnostics.desired_state()     # drift against the applied configuration
```

## A plugin never activates (`PENDING`)

`explain()` prints the requirement-by-requirement verdict and the reason:

```text
consumer (consumer@1.0.0): pending
  memory >=2: no_provider - no provider for 'memory'
  reason: no provider for 'memory'
```

| Status in `explain()` | Meaning | Fix |
| --- | --- | --- |
| `no_provider` | nothing provides that capability | install/provide the capability |
| `version_mismatch` | providers exist, none satisfies the specifier (`registered: old@1.0.0`) | widen the specifier or provide a compatible version |
| `ok` but pending, `reason: dependency cycle` | the requirement is satisfied but graph is cyclic | break the cycle; `plan().raise_for_cycles()` raises `PluginCycleError` with the path |

A plugin failing one *required* capability stays `PENDING`; it is not an error, and
the rest of the composition still activates. An *optional* requirement never blocks.

## `PluginSetupError: plugin 'x' failed during setup`

Setup raised, and everything it created was already reverted (that is guaranteed, not
best-effort). Read the original exception from `__cause__` first; then check:

- `harness.diagnostics.plugins()` - the instance is `FAILED` with its error recorded;
- whether effects leaked despite the rollback: effects must be created through the
  context (`ctx.tools.register`, `ctx.cleanup`, `ctx.create_task`, ...). Anything
  created outside the plugin's scope is not owned, so it is not reverted;
- `ctx.require(...)` for a capability the manifest does not declare in `requires` -
  undeclared requirements are not resolved for you;
- a blocking call where an `await` belongs.

A `BaseException` (for example cancellation during setup) is not wrapped: it
propagates as itself after the rollback.

## `CapabilityNotFound` from a graph node

A node resolved a capability from somewhere other than its run:

```python
async def assistant(state, runtime: Runtime[HarnessRunContext]) -> dict:
    model = runtime.context.require_capability(MODEL)   # this run's generation
```

Resolving from a global, a module-level variable, or a captured object gives you
whatever was current when the module loaded - the opposite of what Chassis
guarantees. If the capability is genuinely absent from the composition, the fix is to
provide it, not to catch the error.

## `CapabilityAmbiguous`

Two providers satisfy one required capability. Decide explicitly, per entry or
globally:

```python
harness.prefer_provider("database", "postgres")          # globally
harness.apply_config({"provider_preferences": {"database": "postgres"}})
```

## A tool call is refused (`PolicyDenied`)

Read `error.context["reason"]`:

| `reason` | Cause | Fix |
| --- | --- | --- |
| a permission string | the policy engine denied it | grant the permission to the tool's owner |
| `"approval"` | `ToolPolicy(approval_required=True)` and the gate refused | provide an `ApprovalGate` that approves, or drop the flag |
| `"hook"` | a `HookEvent.BEFORE_TOOL_EXECUTE` (or `BEFORE_AGENT_RUN`) handler bailed | inspect the hook registration; a truthy `BAIL` return refuses the call |

The default policy in a plain `Harness` is allow-all; `TestHarness` defaults to a
grant policy with no grants, so a permission-requiring tool is denied unless the test
grants it.

## `BudgetExceeded`

`error.context` carries `dimension`, `limit`, `used`, and `requested`. A dimension is
either **enforced** (a guarantee at a Chassis-owned boundary) or **accounted** (it
holds only when an integration reports usage):

| Dimension | Enforcement | Charged at |
| --- | --- | --- |
| `wall_clock_seconds` | enforced | tool execution (deadline) |
| `tool_calls` | enforced | tool execution |
| `child_runs` | enforced | a nested `harness.agents.invoke(...)` from inside a run |
| `model_calls` | accounted | `run_context.budget.record(model_calls=1)` |
| `tokens` | accounted | `run_context.budget.record(tokens=usage)` |
| `estimated_cost` | accounted | `run_context.budget.record(estimated_cost=cost)` |

An accounted dimension raises nothing unless the code that owns the model call
reports it; Chassis cannot observe a call it does not mediate. To see which is which
at runtime:

```python
BudgetDimension.TOKENS.enforcement          # BudgetEnforcement.ACCOUNTED
harness.diagnostics.budgets()["accounted"]  # ['tokens', ...]
```

A nested run inherits the parent's remaining allowance, so a child cannot spend what
the parent does not have.

## Shutdown hangs, or reports cleanup failures

`stop()` waits `shutdown_grace_seconds` (default 30) for active runs, then retires
still-leased generations anyway and reports a `CleanupFailure` per generation
(`"… run(s) still active after …s; retiring anyway"`). One `EffectCleanupError` is
raised at the end carrying every failure; shutdown never aborts halfway.

The usual cause of a stuck lease is an abandoned stream:

```python
async for event in harness.agents.stream("agent", {"messages": [...]}):
    if done:
        break            # the lease is held until the generator is closed
```

The generation is leased for as long as the stream is consumed. Close it
(`await generator.aclose()`) rather than dropping it, or bound shutdown explicitly in
tests with `Harness(shutdown_grace_seconds=0.05)`.

## An old generation is still live

A generation stays live while a run holds a lease, which keeps its plugin instances
and their resources alive. `generation_pressure()` shows which generation, how old it
is, and who is retaining what:

```python
report = harness.diagnostics.generation_pressure()
print(report.to_text())

report.oldest_lease_age_seconds              # age of the oldest outstanding lease
report.instance_generations                  # instance id -> live generations
harness.diagnostics.instance_generations(instance_id)   # newest first
```

The two usual causes are an abandoned stream and a run that never returned:

- a stream is leased for as long as it is consumed; breaking out of
  `harness.agents.stream(...)` without closing the generator holds the lease
  (close it with `await generator.aclose()`);
- a run that is blocked on an external system keeps its generation alive for as long
  as it blocks, so bound it with a `wall_clock_seconds` budget or a tool timeout.

`report.history_evicted` counting up while a generation is still in
`report.generations` is *expected*, not a bug: the bounded diagnostics history evicts
only retired generations, and it never decides liveness. Chassis will not reclaim a
generation a run still holds, and 0.2 adds no age limit that would; the report is
there to make the retention attributable (see
[lifecycle.md](lifecycle.md#generation-pressure)).

## The graph recompiles far more often than expected

The cache key is composed only of build-time inputs: agent, definition version, state
schema, static tool composition, middleware, and declared build-time capabilities.
Anything else - model provider, database, policy, secret provider, tenant - is
runtime-bound and must *not* rebuild the graph.

If you see repeated `graph.compile` events, the cause is one of:

- a **declared** build-time capability version changing (`definition.build_time_capabilities`);
- a different static tool set: `inputs.tools` comes from the generation's tool
  snapshot, so registering or removing tools changes the key - correctly;
- `definition.middleware` labels changing (they are hashed into the key);
- a definition rebuilt per call: build the `AgentDefinition` once.

## Traces are missing from LangSmith

- tracing is off unless configured: set `LANGSMITH_TRACING=true` (or pass
  `LangSmithTelemetry(enabled=True)`) plus the usual LangSmith variables;
- `TestHarness` installs a recording sink, and application-level spans read through
  `harness.telemetry` go to that recording rather than to LangSmith - assert against
  the recording in tests;
- a tracing failure never breaks a run: the span degrades to a no-op and the failure
  is recorded, so a misconfigured tracer looks like silence rather than an exception.

Runs carry `generation_id`, `snapshot_digest`, `chassis_version`, plugin graph hash,
tool schema hash, and graph definition hash, so an unclear trace is usually an
unconfigured tracer rather than missing metadata.

## A value shows up as `<redacted>`, or a secret is *not* redacted

Redaction tracks values as they are read through the secret provider
(`ctx.secrets`, `harness.secrets`). A secret read straight from `os.environ` is
invisible to the redactor and can reach a trace, snapshot, or error string. Always
go through the provider:

```python
api_key = (await ctx.secrets.get("openai.api_key")).reveal()
```

## Everything is `<redacted>` in diagnostics

Diagnostics describe configuration by key, never by value, on purpose. Use
`harness.diagnostics.config()` for the declared shape and `desired_state()` for
drift; neither carries configuration values.

## `HarnessStateError: no runtime generation is available`

`agents.invoke` and `agents.stream` before `start()` are refused: a harness that was
never started has published no generation, so there is nothing to acquire. Enter it
first, and let the context manager close it:

```python
async with harness:                 # start() → reconcile → publish
    result = await harness.agents.invoke("research-agent", {"messages": [...]})
```

## `HarnessStateError: cannot reconcile a harness that is stopped`

Composition is refused once shutdown began, and so is a reconcile that queued behind
a shutdown (`start()` on a stopped harness raises too). Create a new harness rather
than reviving a stopped one - a stopped harness owns nothing.

## `ReplayMismatch`

Replay compares boundary identity (`kind` + canonical `key`) and refuses to answer a
different operation. `error.context` carries `kind`, `key`, and how many interactions
of that kind were recorded. Either the recording predates the code change, or the
operation genuinely changed. To mix recorded and live execution explicitly:

```python
ReplaySession(mode=ReplayMode.REPLAY, fallback=ReplayFallback.LIVE)
```

Replay covers tool and model boundaries only; interrupts, snapshots, and lifecycle
events are recorded as attribution and never answer an operation
([replay.md](replay.md)).

## A scoped plugin never activates, or resolves to the wrong provider

Scoped composition adds three statuses and an origin to the explanation. Ask for
provenance rather than guessing:

```python
harness.diagnostics.explain_requirement("agent", "database", scope="/tenant:acme/research")
harness.diagnostics.explain_scope("/tenant:acme/research")
```

| Status / field | Meaning | Fix |
| --- | --- | --- |
| `no_provider` | nothing anywhere in the composition provides it | install/provide the capability |
| `not_visible` | a provider exists but is not in this scope's lineage (a sibling's or a descendant's) | declare the provider in an ancestor scope, or consume it where it lives |
| `provider_pending` | a visible provider cannot activate itself | fix that provider's own requirement |
| `ambiguous` | a local and an inherited provider are both valid | `prefer_provider(..., consumer=...)` or `(..., scope=...)` |
| candidate `rejection: capability_not_exposed` | a capability view on the path hides it | widen that scope's `capabilities` (intersection is along the whole path) |

An **unexpected inherited provider** is reported with `origin: inherited (<scope>)`;
narrow the consuming scope's view to stop inheriting it. A **missing provider after
narrowing** is the intersection rule: an intermediate scope can hide a capability a
descendant declares ([scopes.md](scopes.md#6-capability-narrowing)).

## An old scope's resources are still alive after removing the scope

Removing a scope removes its entries from desired state and from the next
generation; it does not destroy instances a live generation can reach. That is the
same unload rule as 0.2, applied to a subtree:

```python
harness.composition.remove("/tenant:acme/research")   # uninstalls its entries
await harness.reconcile()

harness.diagnostics.explain_scope("/tenant:acme/research", generation_id=old_id)  # old tree
harness.diagnostics.generation_pressure()    # which generation still retains it
```

`explain_scope(path, generation_id=...)` reads a specific generation, so an operator
can see what the old run still observes. The instance is disposed when the last
lease on that generation is released (see
[lifecycle.md](lifecycle.md#logical-unload-is-not-physical-disposal)).

## `AgentRetired` from `agents.invoke`

An agent published through an `AgentSpec` was removed, so no new run selects it:

```python
harness.agents.is_retired("finance")     # True
harness.agents.spec("finance", revision="17")   # historical revision still reachable
```

Re-publish with `harness.agents.install(AgentSpec(name="finance", revision="18", ...))`
to make it selectable again. Runs already pinned to an old generation keep observing
it until their lease ends (see [agent-composition.md](agent-composition.md)).

## `ConfigurationError: a published agent revision is immutable`

The same `(name, revision)` was published with different content. A revision, once
published, is never mutated in place:

```text
finance@17  →  change composition  →  ConfigurationError
finance@17  →  finance@18          →  harness.agents.replace(spec)
```

Declare a new revision and use `replace` to make it current.

## An agent's tool is invisible, or `ToolNotFound`

An agent scope exposes the tools its own entries and its ancestors contribute,
filtered by its tool view. Check what the scope actually exposes:

```python
harness.diagnostics.explain_scope("/agents/finance").visible_tools
harness.diagnostics.explain_agent("finance").to_dict()["visible_tools"]
```

Common causes: the tool's plugin is declared in a **sibling** scope (never visible),
the scope's `tools` view does not list it, or another scope already registered a tool
with the same process-global name. Share the tool plugin at an ancestor scope instead.

## `ConfigurationError: cannot install into an undeclared composition scope`

`install(..., scope=...)` validates the path against the desired-state tree.
Declare the scope first — creating a scope or narrowing a view marks the harness
dirty, so the next `reconcile()` (or the next asynchronous entry point) publishes
it:

```python
research = harness.composition.child("research")          # /research
research.install(MyPlugin(), entry_id="search")
await harness.reconcile()
```

## A generation changed even though no plugin did

Scope topology is part of composition identity. Adding a scope, removing one,
restricting a capability view, or changing a requirement's selected provider
publishes a new generation even when the mounted instances are identical — a run
observing `generation.scopes` would otherwise see a different composition. Confirm
what moved with a semantic diff instead of guessing from instance ids:

```python
harness.diagnostics.diff_generations(old_id, new_id).to_text()   # SCOPES / PROVIDERS / REQUIREMENTS
harness.diagnostics.diff_generations(old_id, new_id, include_unchanged=True)
```

## A plugin was rebuilt even though I changed nothing

Reuse requires the recomputed semantic identity to equal the identity the instance
was mounted with, and a replacement bumps the entry revision even when the new
implementation is byte-for-byte the same. Ask why instead of guessing:

```python
harness.diagnostics.explain_reuse(old_id, new_id, "search")
# decision: rebuilt
# reasons: config_changed
# changed inputs: config
```

The reasons are a small, explicit vocabulary (`config_changed`,
`implementation_changed`, `dependency_changed`, `scope_visibility_changed`,
`provider_selection_changed`, `capability_contract_changed`, `preference_changed`).
A semantically identical replacement is reported as `unchanged` (semantically
identical, not physically reused) rather than `reused`
([incremental-composition.md](incremental-composition.md)).

## A consumer is rebuilt whenever its provider is replaced

Expected. A consumer captures its capability objects during `setup`, so if its
selected provider changes it must re-resolve against the provider the new generation
publishes; otherwise it would keep using an instance that is about to be disposed.
The diff reports it as `REWIRED` with `dependency_changed`. Keep the provider's
entry id stable so only the nodes that actually depend on its behaviour are rebuilt,
and read `harness.diagnostics.analyze_impact(old, new)` to see the exact set
([migration.md](migration.md#03-04)).

## A shared resource is still alive though the current generation does not contain it

That is structural sharing working as intended: the resource is the same instance an
older, still-leased generation reaches. `generation_pressure()` now says which
generations reach it and why it is retained:

```python
report = harness.diagnostics.generation_pressure()
next(item for item in report.resources if item.entry_id == "postgres").to_dict()
# {"generations": ["gen_0044", "gen_0043"], "retained_by": ["lease", "sharing"], ...}
```

`lease` means a run is still holding a generation that reaches it; `sharing` means
more than one live generation reaches it. It is disposed once no live generation
reaches it ([lifecycle.md](lifecycle.md#generation-pressure)).

## Two generations look identical but have different snapshot digests

They probably describe the same composition built from different runtime instances.
`digest()` is the digest of the whole snapshot record, including
`runtime_instance_ids` and the generation id. Use the semantic digest when you mean
"the same composition":

```python
old.snapshot_for(old_generation).semantic_digest() == new.snapshot_for(new_generation).semantic_digest()
```

A secret-only configuration change is deliberately invisible in `semantic_digest()`
while still forcing a rebuild ([observability.md](observability.md#runtime-snapshots)).

## An example or a snippet from the docs fails

The examples assert what they print, and the test suite runs all of them
(`tests/integration/test_examples.py`), so a failing example is a real regression
rather than stale documentation. Run `uv run pytest` and check `git status` before
assuming the docs are wrong.
