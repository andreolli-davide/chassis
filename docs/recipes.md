# Recipes

Task-shaped answers to "how do I actually wire this into my application". Every
Chassis API here is the real one; anything belonging to another library is marked as
such.

- [Behind a web service](#behind-a-web-service)
- [Per-tenant composition and identity](#per-tenant-composition-and-identity)
- [Swap a provider while runs are in flight](#swap-a-provider-while-runs-are-in-flight)
- [Budget per request, per tenant, per child run](#budget-per-request-per-tenant-per-child-run)
- [Durable runs: checkpointing and resume](#durable-runs-checkpointing-and-resume)

## Behind a web service

One harness per process, not per request: composition is expensive, and
`async with harness` is what makes it start and shut down safely.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from chassis import MODEL, Harness

harness = Harness("harness.yaml")                     # declarative composition
harness.register_plugin_type("openai-model", OpenAIModelPlugin)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with harness:                               # reconcile on start, drain on stop
        yield


app = FastAPI(lifespan=lifespan)


@app.post("/chat")
async def chat(body: ChatRequest) -> dict[str, str]:
    try:
        result = await harness.agents.invoke(
            "research-agent",
            {"messages": body.messages},
            thread_id=body.thread_id,
            user_id=body.user_id,
            tenant_id=body.tenant_id,
            metadata={"request_id": body.request_id},
        )
    except ToolExecutionError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    return {"text": result.text or "", "generation": result.generation_id}
```

Points that matter:

- **enter the harness before invoking it**: `async with harness` (or an explicit
  `await harness.start()`) is what creates the first generation. Once running,
  pending desired-state changes are applied automatically before a run, so
  `install`/`provide` can be called synchronously at any time and the next invocation
  picks them up;
- shutdown is not optional - `harness.stop()` waits for in-flight runs
  (`shutdown_grace_seconds`), then disposes what nothing can reach any more;
- the run's `thread_id` maps to LangGraph checkpointing; `user_id`/`tenant_id` travel
  in the immutable run context, so a graph node can read them from
  `runtime.context`;
- `result.generation_id` is the composition the answer came from - log it, it is what
  makes an incident reproducible.

Expose health from the same authoritative state:

```python
@app.get("/healthz")
async def healthz() -> dict:
    return harness.diagnostics.status()
```

## Per-tenant composition and identity

Identity is per run; composition is per harness. Two workable shapes:

```python
# Per-request identity (the common case): same composition, different tenant.
await harness.agents.invoke("support-agent", {"messages": [...]}, tenant_id=tenant.id)

# Per-tenant wiring: a provider that depends on the tenant, resolved per generation.
config = {
    "version": 1,
    "plugins": [
        {"id": f"db-{tenant.id}", "plugin": "postgres-pool", "config": {"dsn_ref": tenant.dsn_ref}},
        {"id": f"agent-{tenant.id}", "plugin": "support-agent", "requires": {"database": ">=1,<2"}},
    ],
    "provider_preferences": {"database": f"db-{tenant.id}"},
}
```

The second shape is deliberately declarative: entries carry stable ids, a provider
change is a `REPLACE` that publishes a new generation, and runs already in flight keep
the one they acquired ([configuration.md](configuration.md)).

When one capability has several providers, decide explicitly instead of relying on
resolution order:

```python
harness.prefer_provider("database", "postgres")              # globally
entry = "consumer:database"                                  # or per entry
harness.prefer_provider(entry, "postgres")
```

## Swap a provider while runs are in flight

The whole point of generations: reconfiguration never mutates a running composition.

```python
harness.install(OpenAIModelPlugin(model="gpt-x"), entry_id="model", replace=True)
await harness.reconcile()          # publishes generation N+1

# Runs that already acquired generation N keep its model object, its policy, and
# its secret provider until they finish; nothing they hold is disposed meanwhile.
```

`examples/safe_provider_replacement.py` runs exactly this and asserts it, including
that the old provider is disposed **after** the last run releases it. Two rules of
thumb:

- swap by replacing the *entry* (`replace=True`) rather than by mutating an object you
  already handed to the harness;
- if a capability is runtime-bound (model, database, policy, secrets, tenant), a swap
  reuses the compiled graph - it does not recompile it
  ([langgraph.md](langgraph.md)).

## Budget per request, per tenant, per child run

Three levels, from coarsest to finest:

```python
# 1. harness default for every run
harness = Harness(default_budget_limits=BudgetLimits(wall_clock_seconds=60, tool_calls=20))

# 2. this run only
await harness.agents.invoke(
    "research-agent",
    {"messages": [...]},
    limits=BudgetLimits(tool_calls=5, child_runs=2),
)

# 3. a nested run started from inside a run inherits what is left
#    (a graph node calling harness.agents.invoke(...) is a child run)
```

Enforced by Chassis: wall clock, tool calls, child runs. Accounted: `model_calls`,
`tokens`, and `estimated_cost`, because graphs call models and not the harness — they
hold only when the code that owns the call reports usage:

```python
run_context.budget.record(model_calls=1, tokens=usage.total_tokens, estimated_cost=cost)
```

`BudgetDimension.TOKENS.enforcement` and `harness.diagnostics.budgets()` tell you
which is which at runtime, so a token limit is never mistaken for a guarantee.

A `BudgetExceeded` carries `dimension`, `limit`, `used`, and `requested`, so a
service can map it to a 429 rather than a 500. Children can never exceed what the
parent has left, so tenant-wide ceilings hold even through nested agents.

## Durable runs: checkpointing and resume

LangGraph owns durability; Chassis only carries what a run needs to be attributed.
Pass the checkpointer you want (any LangGraph checkpointer; install
`langgraph-checkpoint-postgres` for `AsyncPostgresSaver`):

```python
harness.register_agent(
    LangGraphAgent(definition, checkpointer=AsyncPostgresSaver(pool), store=store)
)
```

Then a run and its resume are two ordinary calls:

```python
first = await harness.agents.invoke("research-agent", {"messages": [...]}, thread_id="t-1")

if first.interrupted:
    approved = await harness.agents.invoke(
        "research-agent", {"messages": []}, thread_id="t-1", resume=first.resume_values()
    )
```

`examples/basic_agent.py` does this end to end with `InMemorySaver`, including
streaming and the interrupt payload a UI would render.

Two caveats worth knowing before you scale it: a `thread_id` is a concurrency key -
two concurrent runs on the same thread are a LangGraph problem, not something Chassis
serializes - and an abandoned `agents.stream(...)` generator holds its generation
lease until it is closed.
