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

`error.context` carries `dimension`, `limit`, `used`, and `requested`. Enforced
dimensions and where they are charged:

| Dimension | Enforced at |
| --- | --- |
| `wall_clock_seconds` | tool execution (deadline) |
| `tool_calls` | tool execution |
| `child_runs` | a nested `harness.agents.invoke(...)` from inside a run |
| `model_calls`, `tokens`, `estimated_cost` | declared only - graphs call models, not the harness |

`tokens` and `estimated_cost` raise nothing unless the code that owns the model call
records them: `run_context.budget.consume(BudgetDimension.TOKENS, amount=usage)`.
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

## An example or a snippet from the docs fails

The examples assert what they print, and the test suite runs all of them
(`tests/integration/test_examples.py`), so a failing example is a real regression
rather than stale documentation. Run `uv run pytest` and check `git status` before
assuming the docs are wrong.
