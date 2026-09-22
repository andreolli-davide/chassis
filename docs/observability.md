# Observability

Chassis instruments the operations it owns and leaves model, tool, and graph
internals to LangGraph, LangChain, and LangSmith. It does not create a competing
telemetry universe.

## The signal contract

Every span and event Chassis emits is declared in
`chassis.telemetry.signals.SIGNALS`: a stable name, whether it is a span or an
event, the required attribute keys, and the optional ones. The contract is
backend-independent and enforced by `tests/telemetry/test_signal_contract.py`
against the in-repo recording backend — adapters transport these signals, they
do not reshape them (guarantee G27).

| Signal | Kind | Required attributes | Emitted for |
| --- | --- | --- | --- |
| `harness.reconcile` | span | `harness` | one reconciliation (failure recorded on the span) |
| `harness.shutdown` | span | `harness` | one shutdown run |
| `plugin.mount` | span | `plugin`, `entry_id` | plugin setup for a candidate instance |
| `plugin.unmount` | span | `plugin`, `entry_id`, `instance_id` | plugin teardown and scope close |
| `tool.execute` | span | `tool`, `generation_id` | one tool call (attributes include `registration_id`, `run_id`, `replayed`) |
| `agent.run` | span | `agent`, `agent_revision`, `generation_id` | one agent invocation or stream |
| `dependency.resolve` | event | `eligible`, `pending`, `cycles`, `edges`, `scopes` | one resolution outcome |
| `generation.build` | event | `plugins`, `mounted` | a candidate generation was assembled |
| `generation.publish` | event | `generation_id`, `sequence`, `previous`, `plugins` | a generation became current |
| `generation.acquire` | event | `generation_id`, `sequence` | a run leased the current generation |
| `generation.release` | event | `generation_id`, `leases` | a run released its lease |
| `generation.draining` | event | `generation_id`, `successor`, `leases` | the previous generation began draining |
| `generation.retired` | event | `generation_id` | a draining generation retired |
| `generation.impact` | event | — | reuse/rebuild decision counts |
| `policy.decision` | event | `tool`, `allowed` | one authorization decision |
| `budget.exhausted` | event | `tool`, `dimension` | a budget dimension was exhausted |
| `replay.hit` | event | `kind` | a recorded boundary answered the operation |
| `replay.miss` | event | `kind` | no record exists for the boundary key |
| `replay.exhausted` | event | `kind` | the key's records were consumed |
| `graph.compile` | event | `agent`, `definition_version` | a graph was compiled and cached |
| `graph.cache` | event | `result` (`hit`/`miss`/`evict`) | one cache lookup or eviction |
| `graph.cache.invalidate` | event | `agent`, `entries` | cached graphs were invalidated |
| `hook.failure` | event | `event`, `error_type` | a hook handler failed and was recorded |
| `cleanup.failure` | event | `error_type` | a disposer or teardown step failed and was aggregated |
| `telemetry.failure` | event | `operation`, `signal` | a telemetry backend failed and was contained |

In 0.9.0 the retirement event was renamed `generation.drain` →
`generation.retired` to match the lifecycle vocabulary; names are stable from
0.9.0 on.

### Correlation

The correlation fields — `run_id`, `thread_id`, `generation_id`,
`snapshot_digest`, `agent`, `agent_revision`, `entry_id`, `instance_id`,
`registration_id`, `scope_id` — are attached at the boundary that owns the
fact (`CORRELATION_FIELDS` in the contract): `agent.run` carries the snapshot
digest and revision, `tool.execute` carries the generation, run, and tool
registration ids, `plugin.*` carries entry and instance ids. Downstream systems
join on these fields; they never need to parse names or prose.

### Cardinality and payload rules

Attribute values are scalars (string, number, bool, null) or short bounded
sequences of scalars: at most `ATTRIBUTE_ITEMS_LIMIT` items and
`ATTRIBUTE_STRING_LIMIT` characters. Raw payloads — requests, responses,
messages, prompt bodies, configuration material — are **forbidden** in signals;
`validate_signal()` rejects them, and the contract test fails any emission that
carries one. Identity values (run ids, boundary key digests) are high-cardinality:
correct as span attributes, never as metric labels.

### Redaction

Everything emitted through the harness passes the redaction boundary first
(`SafeTelemetry(RedactingTelemetry(...))` around every backend), so sensitive
attribute keys and learned secret values cannot reach any signal. The contract
test asserts absence of the secret material across every recorded attribute,
not merely the presence of `<redacted>`.

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

## Enabling OpenTelemetry

The OpenTelemetry adapter (`pip install "chassis-harness[opentelemetry]"`) maps
the same signal contract onto OpenTelemetry: Chassis spans become OpenTelemetry
spans nested in the ambient context, Chassis events become events on the active
span (or zero-duration spans when none is active), and names, attributes, and
redaction are exactly the contract above. It is a separate adapter from
LangSmith — both can run together.

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from chassis import Harness
from chassis.telemetry import LangSmithTelemetry, OpenTelemetryTelemetry, TeeTelemetry

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)

harness = Harness(
    telemetry=TeeTelemetry(
        OpenTelemetryTelemetry(provider.get_tracer("chassis")),
        LangSmithTelemetry(),
    )
)
```

`OpenTelemetryTelemetry()` with no arguments uses the ambient tracer under the
`chassis` instrumentation name, so a Chassis span nests inside the trace the
application already has. Attributes and recorded errors are scrubbed by the
adapter before export, and — like every backend — it is wrapped by the harness
in `SafeTelemetry(RedactingTelemetry(...))`, so a failing collector can never
break the operation being observed (`tests/telemetry/test_otel_adapter.py`
exports through the SDK's in-memory exporter and asserts exactly this
mapping).

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

Telemetry can never break what it observes: every backend runs inside a safe
wrapper (`SafeTelemetry`) that contains failures at span enter, update, error,
exit, and event time — one backend can neither abort the operation nor suppress
another backend in a `TeeTelemetry` fan-out. Contained failures stay visible
through the wrapper's `failures` counter and the `chassis.telemetry` logger, and
the harness wires this chain (`SafeTelemetry(RedactingTelemetry(...))`) around
every configured backend. A registered runtime that accepts harness services
(`bind_harness_services`, e.g. `LangGraphAgent`) adopts the harness telemetry
and redaction at registration unless it was constructed with an explicit
override.

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
  "format_version": 1,
  "chassis_version": "0.9.0",
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
`semantic_scopes` names the provider *entry* ids instead, so it survives a
separate materialisation of the same composition. Both are part of the record:
`to_dict()` declares its serialization format version (`SNAPSHOT_FORMAT_VERSION`,
never the package version) and `RuntimeSnapshot.from_dict()` rebuilds an
identical record — migrating or explicitly rejecting pre-versioning, future, or
corrupted payloads ([compatibility.md](compatibility.md#persisted-formats)).

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
