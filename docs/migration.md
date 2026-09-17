# Migrating from 0.1

Chassis is pre-1.0, and 0.2 uses that freedom to remove ambiguity. This page lists
every change that can break a 0.1 caller, why it was made, and what to do instead.
Nothing here changes lifecycle behaviour: published generations are still immutable,
publication is still transactional, and logical unload is still distinct from
physical disposal.

## Installation

The core no longer depends on `langgraph`, `langchain-core`, or `langsmith`.

```bash
# 0.1
pip install chassis-harness

# 0.2
pip install chassis-harness                        # core only
pip install "chassis-harness[langgraph]"           # LangGraph adapter + langchain-core
pip install "chassis-harness[langsmith]"           # LangSmith telemetry + evaluation
```

If you use `chassis.langgraph`, `chassis.replay.ReplayChatModel`, `FakeChatModel`,
`fake_tool`, or any `langchain-core` model or tool, install the `langgraph` extra.
If you use `LangSmithTelemetry` with tracing enabled, or `evaluate_agent`, install
the `langsmith` extra. Importing those without the extra raises a `MissingExtraError`
that names the extra to install.

## `ToolSnapshot.to_langchain_tools()` → `to_tools()`

The core no longer names a specific tool library, so the accessor no longer does
either. The returned objects are unchanged.

```python
# 0.1
tools = snapshot.to_langchain_tools()

# 0.2
tools = snapshot.to_tools()
```

A tool registered with the harness must now satisfy the structural
`chassis.tools.Tool` protocol — a `name`, a `description`, and an awaitable
`ainvoke` — rather than being an instance of `langchain_core.tools.BaseTool`. A
`BaseTool` satisfies the protocol unchanged, so existing tools keep working; the
change only removes the import-time dependency.

## `GenerationManager.acquire()` / `release()` → `acquire_lease()` / `release_lease()`

Leases now carry identity so that lease *age* is authoritative. `Harness.acquire()`
is unchanged: it still yields the acquired `RuntimeGeneration`.

```python
# 0.1
generation = manager.acquire()
manager.release(generation)

# 0.2
lease = manager.acquire_lease()
manager.release_lease(lease)          # lease.generation is the generation
```

Most code uses `async with harness.acquire() as generation:` and needs no change.

## Budget enforcement is now explicit

`BudgetLimits(...)` and the enforced/accounted split are unchanged in shape, but the
API now states which is which:

- `wall_clock_seconds`, `tool_calls`, `child_runs` are **enforced**: a configured
  limit is a guarantee at a Chassis-owned boundary.
- `model_calls`, `tokens`, `estimated_cost` are **accounted**: a configured limit is
  intent, and holds only when the integration that owns the call reports usage with
  `run_context.budget.record(model_calls=…, tokens=…, estimated_cost=…)`.

If you configured a token or cost limit in 0.1 expecting automatic enforcement,
0.2 makes the gap visible: `BudgetDimension.TOKENS.enforcement`,
`BudgetLimits.accounted_dimensions()`, `governor.to_dict()["enforcement"]`, and
`harness.diagnostics.budgets()` all report `accounted`. Add a `record(...)` call at
the point where the model response is received. See
[Budgets](plugin-author-guide.md#budgets) for a worked example.

## Nothing removed from the lifecycle

`Harness`, `Scope`, `Plugin`, plugin manifests, the resolver, reconciliation,
`RuntimeGeneration`, run contexts, and diagnostics keep their 0.1 shape. Everything
new — generation pressure, lease age, budget enforcement metadata, the `Tool`
protocol — is additive unless listed above.
