# Agent composition

Chassis 0.5 adds a first-class, versioned description of what runtime
composition an agent sees: `AgentSpec`. It is a *thin* layer over the
kernel — it materializes into composition scopes, resolves through the same
resolver, reuses through the same semantic identity, and publishes through the same
immutable generations. It does not execute anything.

> Chassis may define what an agent is composed of and which immutable revision a run
> executes against, but it does not decide how an agent thinks.

Agent loop, planning, memory, reasoning strategy, workflow logic, and user
authorization remain the responsibility of higher layers or integrations.

## 1. Why AgentSpec exists

Chassis already composes *runtime*: plugins, capabilities, scopes, generations. The
missing piece was a named, versioned description of the composition a *logical
agent* should see, so that systems with multiple agent roles can share one kernel
without hand-assembling a scope tree for every role.

`AgentSpec` answers one question — *what runtime composition does this agent see?* —
and nothing else:

- a logical name and an author-declared revision;
- the scope path it materializes into;
- the runtime it binds to (by a logical reference, never a concrete engine type);
- the capabilities it may observe, and the requirements it declares;
- the tools it may observe;
- the plugin contributions it owns;
- an engine-neutral profile label and free-form metadata.

## 2. AgentSpec vs AgentDefinition

The two concepts are deliberately separate:

```text
AgentSpec
    = what runtime composition an agent sees   (Chassis, engine-neutral)

AgentDefinition
    = how a specific execution engine builds and runs the graph
      (chassis.langgraph, LangGraph-specific)
```

`AgentSpec` never imports LangGraph. It references a runtime through
`runtime_ref`, a logical name resolved against the harness's registered runtimes.
For LangGraph, the referenced runtime is a `LangGraphAgent` built from an
`AgentDefinition`:

```python
from chassis import Harness
from chassis.agent_spec import AgentSpec
from chassis.langgraph import AgentDefinition, LangGraphAgent

harness = Harness()
harness.register_agent(
    LangGraphAgent(
        AgentDefinition(name="finance-graph", version="7", state_schema=State, build=build)
    )
)
harness.agents.install(
    AgentSpec(name="finance", revision="17", runtime_ref="finance-graph")
)

result = await harness.agents.invoke("finance", {"messages": [...]})
assert (result.agent, result.agent_revision) == ("finance", "17")
```

The graph's own name and the logical agent's name are independent: the spec binds
them, and attribution follows the agent. The core remains importable without the
LangGraph extra; see [langgraph.md](langgraph.md).

## 3. Agent identity and revision

Identity is two-level:

```text
agent name:  finance          (stable logical identity)
revisions:   finance@17       finance@18       finance@19
```

- the **name** identifies the agent in invocation, diagnostics, and snapshots;
- the **revision** is an explicit author declaration: *this is a new authored agent
  definition*.

A revision, once published, is never mutated in place. Publishing the same
`(name, revision)` with different content is rejected; a composition-affecting
change is a **new revision**. A revision change is not collapsed merely because two
revisions materialize to a semantically equivalent composition — the distinction is
preserved exactly as it is for plugin revisions.

## 4. Agent → CompositionScope materialization

There is exactly one scope model. An `AgentSpec` does not introduce `AgentScope`; it
materializes into the existing
[composition primitives](scopes.md):

```text
AgentSpec finance@17
        ↓
CompositionScope /agents/finance
        ↓
plugin contributions · capability view · tool view · requirements · metadata
        ↓
candidate generation → resolve → publish immutable generation
```

`harness.agents.install(spec)` declares that composition as desired state:

- the scope is created on demand (default `/agents/<name>`);
- `capabilities` narrows the scope's capability view (`None` exposes everything);
- `tools` narrows the scope's tool view (`None` exposes every visible tool);
- `requires`/`optional` become scope requirements, resolved with provenance;
- `plugins` become plugin entries owned by the scope, resolved through the plugin
  catalog (or supplied directly for programmatic specs);
- reserved metadata records the agent name and revision the scope materialized.

A published generation therefore carries which revision it materialized, which is
what makes a run attributable to `generation + agent + revision`.

```python
harness.agents.install(
    AgentSpec(
        name="finance",
        revision="17",
        runtime_ref="finance-graph",
        capabilities=["model", "database", "tools"],
        requires={"model": ">=1,<2", "database": ">=1,<2"},
        tools=["spreadsheet"],
        profile="reasoning",
        plugins={"ledger": {"region": "eu"}},
    )
)

await harness.reconcile()
scope = harness.current_generation.scopes.get("/agents/finance")
assert scope.metadata["chassis.agent_revision"] == "17"
```

Materialization is transactional and owner-safe: contributions are staged and
validated before desired state is mutated, a failure restores the exact previous
scope and registry state, and a revision that moves scope withdraws the old one    only after the new one materialized. `install` refuses to take over a
pre-existing user-owned scope — an agent owns its `/agents/<name>` scope (or one    its previous revision created) and nothing else — and the `chassis.agent` and
`chassis.agent_revision` metadata keys are reserved for the harness.

## 5. Tools and capabilities

Both are **composition visibility**, and both use the same inheritance-plus-narrowing
rule a scope already applies:

- a scope sees its own and its ancestors' providers, filtered by the intersection of
  every capability view along its path;
- a scope sees its own and its ancestors' tools, filtered by the intersection of
  every tool view along its path;
- siblings never see each other's local providers or tools.

A tool visible to an agent is **not** a tool authorized for the current user or
action; see [Security and the authorization boundary](#10-security-and-the-authorization-boundary).
Chassis does not build a second tool system: the structural
[`Tool`](plugin-author-guide.md) protocol and the scoped tool registry are
unchanged.

## 6. Revision pinning

A run is attributable to both its generation and its agent revision, and it remains
associated with the revision it selected when it started:

```text
run A starts          generation 101   agent finance@17
new revision published:               finance@18
run B starts          generation 102   agent finance@18

run A continues:      generation 101   agent finance@17   (unchanged)
```

Invocation reconciles pending changes, acquires the generation, and reads the agent
revision that generation actually materialized. If a concurrent publication slips
in, the generation wins: a run is attributed to the composition it acquired, never
to one that replaced it. A runtime may report its own name, but the wrapper stamps
the logical agent and revision on the run context, result, and snapshot.

```python
result = await harness.agents.invoke("finance", {"messages": [...]})
assert result.agent_revision == "17"
assert harness.run_snapshot(context).agent_identity == "finance@17"
```

## 7. Retirement

`harness.agents.remove(name)` means *no new execution selects this agent*:

```python
assert harness.agents.remove("finance") is True
# New runs refuse it:
with pytest.raises(AgentRetired):
    await harness.agents.invoke("finance", {"messages": [...]})
```

Retirement maps onto the existing generation lifecycle — there is no second
resource state machine:

```text
ACTIVE ──▶ RETIRED ──▶ no new runs
                        old generation/revision still reachable
                        ↓ final lease ends
                        resources become disposable
```

The active revision's contributions leave desired state, so the next generation no
longer contains them. Published generations, and the resources they reach, are
unaffected: a run holding an older generation keeps observing it until its lease is
released, and a resource is disposed only when no live generation reaches it (G6,
G7, G19). Historical revisions stay reachable for diagnostics and diffing.

## 8. Incremental reuse between revisions

Agent revisioning does not bypass semantic identity. A revision that leaves a
contribution unchanged does not reinstall its entry, so its mounted instance is
eligible for reuse under the 0.4 rules:

```python
# finance@17: ledger, model=reasoning
harness.agents.replace(
    AgentSpec(
        name="finance",
        revision="18",
        plugins={"ledger": {}, "web": {}},   # ledger unchanged, web added
    )
)
result = await harness.reconcile()
assert result.mounted == ("agent:finance:web",)   # only the new contribution
```

Unchanged nodes are the exact runtime instances they already were, the shared
provider is retained, and the affected scope rebuilds only the dependency closure
that actually changed. See
[incremental-composition.md](incremental-composition.md).

## 9. Diagnostics

```python
explanation = harness.diagnostics.explain_agent("finance", revision="17")
print(explanation.to_text())
```

```text
agent: finance@17
  scope: /agents/finance
  runtime: finance-graph
  profile: reasoning
  capabilities: database, model, tools
  tools: spreadsheet
  plugins: ledger
  visible tools: spreadsheet
  providers:
    database: ledger
    model: model-v1
  requirements:
    model <2,>=1: ok
    database <2,>=1: ok
  composition digest: 4d8c…
  published in: gen_0004
```

```python
diff = harness.diagnostics.diff_agents("finance", "17", "18")
print(diff.to_text())
```

`diff_agents` compares two revisions structurally (scope, runtime, profile,
capabilities, tools, plugin contributions, requirements) and delegates the runtime
reuse/rebuild analysis to the existing generation impact engine, reporting
`COMPOSITION IMPACT` for the generations each revision published.

## 10. Security and the authorization boundary

An `AgentSpec` describes a **composition ceiling**, not authority. The ceiling is
enforced at run time: `HarnessRunContext.require_capability()` and graph
build-time capability versions observe only the registrations the acquired
scope's view exposes — in both the invoke and the stream path — and an unknown
scope path is rejected rather than silently exposing an empty view. What the
ceiling defines:

```text
AgentSpec defines:
  which plugins, providers, capabilities, and tools a scope may observe

External authorization defines:
  whether a concrete action is allowed right now
```

`capabilities` and `tools` narrow visibility using the same rules a scope already
uses. They do not grant a user or organization anything, and Chassis adds no RBAC,
policy rewrite, or IAM model in this release. Secrets keep flowing through the
existing secret provider and redaction boundaries (G11); `AgentSpec.to_dict()`,
`explain_agent`, and snapshots report configuration and metadata **by key**, never
by value.

## 11. LangGraph integration

The adapter changes only enough to connect the two concepts:

- locate the runtime a spec references (`runtime_ref` → a registered
  `LangGraphAgent`);
- run it against the generation and scope associated with that spec (the run
  environment's tools are narrowed to the agent's scope);
- preserve generation and revision attribution through results, streamed events,
  trace metadata, and snapshots.

Nothing LangGraph-specific moves into the core, and `chassis` still imports without
the extra.

## 12. Limitations

- **No agent loop, planner, memory, RAG, browser, workflow DSL, or MCP framework.**
  Those are outside Chassis core by design.
- **No user/organization authorization.** Composition visibility is not IAM.
- **No autonomous publisher.** A programmatically created `AgentSpec` passes the
  same validation as an authored one; Chassis does not publish revisions on its own.
- **No child-agent delegation.** The model does not assume one run equals exactly
  one agent forever, but `spawn_agent`/`delegate` and an agent tree are not part of
  this release.
- **No distributed registry or persistent cross-process composition cache.**
  Revisions and reuse are per process.
- **A revision is not automatically collapsed.** Two revisions that materialize to a
  semantically equivalent composition remain distinct.
- **Tool names are process-global.** Two agent scopes cannot contribute the same
  tool name; declare a shared tool plugin at an ancestor scope (both inherit it) or
  give each agent a distinct tool name.
- **A shared scope has one owner.** A scope an agent created is removed when that
  agent retires; declaring the same explicit `scope` for two agents is not
  supported.

## Worked example

A research, finance, and support agent sharing infrastructure but with distinct
tools, capabilities, and requirements:

```text
root                    shared database, telemetry
├── /agents/research    view: model, database, tools   tools: web-search
├── /agents/finance     view: model, database, erp     tools: spreadsheet
└── /agents/support     view: model, tools             tools: ticket-lookup
```

```python
harness.register_plugin_type("search", search_plugin)
harness.register_plugin_type("ledger", ledger_plugin)
harness.register_plugin_type("tickets", tickets_plugin)

harness.agents.install(AgentSpec(
    name="research", revision="7", runtime_ref="research-graph",
    capabilities=["model", "database", "tools"],
    requires={"model": ">=1,<2", "database": ">=1,<2"},
    tools=["web-search"], profile="reasoning", plugins={"search": {}},
))
harness.agents.install(AgentSpec(
    name="finance", revision="17", runtime_ref="finance-graph",
    capabilities=["model", "database", "erp"],
    requires={"model": ">=1,<2", "database": ">=1,<2", "erp": ">=1,<2"},
    tools=["spreadsheet"], profile="reasoning", plugins={"ledger": {}},
))
harness.agents.install(AgentSpec(
    name="support", revision="3", runtime_ref="support-graph",
    capabilities=["model", "tools"],
    requires={"model": ">=1,<2"},
    tools=["ticket-lookup"], profile="fast", plugins={"tickets": {}},
))
```

The full, executed version is
[examples/agent_composition.py](https://github.com/andreolli-davide/chassis/blob/main/examples/agent_composition.py),
which also shows a revision change reusing an unchanged contribution and a run
staying pinned to its revision.
