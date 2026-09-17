# Observability

Chassis instruments the operations it owns and leaves model, tool, and graph
internals to LangGraph, LangChain, and LangSmith. It does not create a competing
telemetry universe.

## What is traced where

| Operation | Instrumented by | Signal |
| --- | --- | --- |
| model call | LangChain/LangGraph | native LangSmith run |
| tool execution | LangChain/LangGraph | native run, plus `chassis_tool*` metadata |
| graph execution | LangGraph | native run |
| reconcile | Chassis | span `harness.reconcile` |
| plugin mount / unmount | Chassis | spans `plugin.mount`, `plugin.unmount` |
| shutdown | Chassis | span `harness.shutdown` |
| agent invocation | Chassis | span `agent.run` |
| dependency resolution | Chassis | event `dependency.resolve` |
| generation build / publish / drain | Chassis | events |
| graph compile / cache | Chassis | events `graph.compile`, `graph.cache` |
| policy decision | Chassis | event `policy.decision` |
| budget exhaustion | Chassis | event `budget.exhausted` |

## Enabling LangSmith

The backend lives behind the `langsmith` extra
(`pip install "chassis-harness[langsmith]"`). Importing it without the extra is
fine — the class loads lazily — but constructing an *enabled* backend, or calling
`evaluate_agent`, raises a `MissingExtraError` naming the extra.

```python
from chassis import Harness
from chassis.telemetry import LangSmithTelemetry

harness = Harness(telemetry=LangSmithTelemetry())   # enabled by LANGSMITH_TRACING
```

`LangSmithTelemetry` is enabled automatically when `LANGSMITH_TRACING` or
`LANGCHAIN_TRACING_V2` is set, so an application that already configured tracing
gets Chassis spans for free.

- Spans nest under the ambient run, so a plugin mount appears inside the trace of
  the run that caused it.
- Events attach to the ambient run; without one they are dropped rather than
  inventing a parentless trace.
- Tracing failures are logged and the traced operation continues. Observability is
  not a single point of failure unless you set `raise_on_error=True`.

## Fan-out

`TeeTelemetry` sends one signal to several backends, which is how an OpenTelemetry
exporter, a recording sink, and LangSmith can coexist:

```python
harness = Harness(telemetry=TeeTelemetry(LangSmithTelemetry(), my_otel_backend))
```

Any object implementing the two-method `Telemetry` protocol works; Chassis does not
require a specific vendor SDK.

## Generation pressure metrics

Generation lifetime is observable as structured data, so a metrics backend can export
it without Chassis depending on that backend. `generation_pressure()` reads
authoritative state (the live generation set, each generation's lease table, and the
instances each generation was published with) and performs no `await`, so it is cheap
enough to scrape:

```python
report = harness.diagnostics.generation_pressure()

report.metrics()
# {
#   "chassis.generations.live": 4.0,
#   "chassis.generations.draining": 3.0,
#   "chassis.generations.leases": 3.0,
#   "chassis.generations.oldest_lease_age_seconds": 862.0,
#   "chassis.resources.shared": 1.0,
# }

report.to_dict()   # full structured form, including per-generation retained plugins
report.to_text()   # the human-readable rendering
```

Feed it to whatever exporter you already run:

```python
harness = Harness(telemetry=TeeTelemetry(my_otel_backend, LangSmithTelemetry()))
for name, value in harness.diagnostics.generation_pressure().metrics().items():
    my_otel_backend.gauge(name, value)
```

Chassis reports pressure; it does not act on it. There is no default age limit, and
nothing is reclaimed because it is old — a loitering generation is visible and
attributable, never silently destroyed
([lifecycle.md](lifecycle.md#generation-pressure)).

Budget enforcement is observable the same way: `harness.diagnostics.budgets()`
returns the configured defaults with each dimension's `enforcement`
(`enforced`/`accounted`), and `BudgetGovernor.to_dict()` carries the same for a live
run. A token or cost limit reported as `accounted` holds only when the integration
reports usage ([plugin-author-guide.md](plugin-author-guide.md#budgets)).

## Redaction

Secret values must not reach traces, snapshots, diagnostics, replay records, or
exception strings.

```python
harness.redactor          # SecretRedactor shared by telemetry, snapshots, tools
```

Three mechanisms cooperate:

1. `RedactingSecretProvider` wraps the configured provider, so every value it hands
   out is registered with the redactor the moment it is resolved.
2. Tool-boundary errors, traces, and trace metadata are redacted before they leave.
3. Configuration is redacted by key name as well as by value, so an API key sitting
   in a plugin config is redacted even if nothing ever resolved it.

`SecretValue` refuses to render itself: `repr`, `str`, and diagnostics show
`<redacted>`, and material leaves only through an explicit `reveal()` at the point
of use.

## Runtime snapshots

Every run is attributable to an immutable snapshot:

```python
snapshot = harness.snapshot_for(harness.current_generation, agent="research")
snapshot.to_dict()
snapshot.digest()            # the whole record
snapshot.semantic_digest()   # semantic composition only
snapshot.physical_digest()   # runtime instance ids
```

```json
{
  "chassis_version": "0.5.0",
  "generation_id": "gen_0004",
  "sequence": 4,
  "agent_runtime": "langgraph",
  "plugins": {"chassis-services": "1.0.0", "research-tools": "1.0.0"},
  "capabilities": {"model": ["1"], "tools": ["1"]},
  "config_hash": "...",
  "plugin_graph_hash": "...",
  "tool_schema_hash": "...",
  "graph_definition_hash": "...",
  "prompt_hash": null,
  "runtime_instance_ids": ["plugin_8f3a…", "plugin_c21b…"],
  "scopes": {
    "root": "/",
    "scopes": [
      {"path": "/", "parent": null, "children": ["/research"], "capabilities": null,
       "entries": ["postgres"], "providers": {"database": ["plugin_…"]},
       "selections": [{"consumer": "agent", "requirement": "database <2,>=1",
                       "status": "resolved", "provider": "postgres", "provider_scope": "/"}]},
      {"path": "/research", "parent": "/", "children": [], "capabilities": ["database", "model"],
       "entries": ["agent"], "providers": {}, "selections": []}
    ]
  }
}
```

`scopes` identifies the scope tree, the local providers of each scope, the
capability view in effect, and the resolved selection of every requirement, using
identities and instance ids only. Scope *metadata* is never included, and
configuration values never appear anywhere in a snapshot.

The `scopes` field names the runtime instance ids that provide a capability;
`semantic_scopes` (used by `semantic_digest()`, not emitted in `to_dict`) names the
provider *entry* ids instead, so it survives a separate materialisation of the same
composition.

**Scope structure is part of the digest, on purpose.** Topology and per-requirement
selection are observable through the generation a run acquires: two generations
whose scope trees differ would behave differently for a run that reads
`generation.scopes`, so their digests must differ. Republishing an unchanged
composition reuses the current generation and therefore reproduces the identical
digest.

**Semantic identity and physical identity are separate.** `digest()` covers the
whole record, including `runtime_instance_ids`; `semantic_digest()` covers only the
semantic composition, so two generations that describe the same composition but were
materialised separately share it; `physical_digest()` covers the runtime instance
ids. A secret-only configuration change is invisible in `semantic_digest()` but
still rebuilds the node, because the private identity fingerprint is computed from
the effective configuration
([incremental-composition.md](incremental-composition.md#semantic-sameness-is-not-physical-reuse)).

Snapshots contain **no configuration values**. Configuration is represented by a
hash computed over the redacted payload, so a snapshot explains composition
without being a place secrets could leak from -- and the test suite asserts exactly
that.

`agent.run` spans and `AgentResult.metadata["snapshot_digest"]` carry the digest,
which is what ties a trace to an exact generation.

## Hashing rules

Canonical serialization is documented so a digest means the same thing everywhere:

1. reduce the payload to JSON-compatible data;
2. sort mapping keys;
3. keep sequence order, sort sets;
4. serialize as UTF-8 with compact separators;
5. digest with SHA-256.

Values whose representation is not stable are **rejected** rather than stringified:
a hash built from `repr` or object identity would silently differ for identical
logical state. Integral floats normalize to integers so `1.0` and `1` cannot
produce different digests.

Separate hashes exist per concern, because one giant digest whose invalidation
semantics cannot be explained is useless for debugging:

| Hash | Covers |
| --- | --- |
| `config_hash` | redacted plugin configuration, by entry |
| `plugin_graph_hash` | plugin dependency graph nodes and edges |
| `graph_definition_hash` | compiled-graph cache key for the run's agent |
| `tool_schema_hash` | tool names, descriptions, argument schemas |
| `prompt_hash` | a prompt body, when supplied |
| `scopes` (in `to_dict`) | scope topology, capability views, local providers, and requirement selections |
| `semantic_digest()` | semantic composition only: plugins, capabilities, redacted config, dependency edges, tool contracts, semantic scope tree |
| `physical_digest()` | the runtime instance ids a generation was published with |

Composition decisions are also explainable without reading a snapshot:
`harness.diagnostics.explain_requirement(...)`, `explain_scope(...)`, and
`diff_generations(...)` read the same authoritative state
([scopes.md](scopes.md#diagnostics)).

## Evaluation

LangSmith remains the experiment platform; Chassis supplies attribution.

```python
from chassis.evaluation import agent_target, composition_metadata, evaluate_agent

target = agent_target(harness, "research")            # example -> output
metadata = composition_metadata(harness, agent="research")
await evaluate_agent(harness, "research", data=dataset, evaluators=[correctness])
```

`composition_metadata` reports the generation, chassis version, plugins,
capabilities, and hashes, so two experiments can be compared knowing exactly which
composition each ran against. The target also returns the generation it ran on, so
results stay attributable even when composition changed mid-experiment.
