# Production reference application: the order support desk

One self-contained application that composes every Chassis capability into a
production-shaped system, running entirely on deterministic local fakes — no
credentials, no network, no external services. Running it is the verification:
it executes its own assertions and fails loudly if any scenario breaks.

```bash
uv run python -m examples.production_reference.app
```

## Architecture

| Piece | Role | Demonstrates |
| --- | --- | --- |
| `orders-db` plugin | in-memory order repository | declarative configuration, a capability provider (`database@1`), config-by-key observability |
| `support-tools` plugin | `lookup_order` and `notify_customer` tools | capability requirement binding, tool registration with `ToolPolicy`, secret resolution through the redaction seam (`ctx.secrets`) |
| `flaky-analytics` plugin | fails in `setup` on purpose | controlled failure, transactional rollback |
| `analytics` scope | child composition scope | scoped composition: capability and tool narrowing (`select_tools`) |
| `support-agent@1` (`AgentSpec`) | the support-desk agent | AgentSpec materialization, requirements and preferences, revision attribution |
| `support-graph` (`LangGraphAgent`) | tool-calling LangGraph graph | agent invoke and streaming over the runtime users adopt |
| `GrantPolicy({"orders.read"})` | explicit allow-list | policy decisions and denial |
| `StaticSecretProvider` | one fake secret | secret resolution and redaction in every export |
| `ReplaySession` | recording and replay | record → JSON → replay with zero live calls |
| `Harness.preview()` | dry-run planning | the machine-readable plan before every change |

## Execution flow

1. **Planning first.** `harness.preview(CONFIG)` predicts two additions and a
   publication — and mutates nothing (the pending flag is unchanged).
2. **Declarative configuration + scopes.** The config is applied atomically;
   an `analytics` child scope narrows its tool view to `lookup_order`.
3. **Ambiguity, then preference.** Two `database` providers make
   `support-tools`' requirement ambiguous — preview reports it as a `reject`
   action with candidates; `prefer_provider` settles it.
4. **Agent materialization.** `support-agent@1` materializes through the same
   scoped resolver; `explain_agent` confirms its identity.
5. **Invoke.** A tool-calling run goes through policy (`orders.read` granted)
   and answers with full attribution (generation + revision).
6. **Streaming.** Every streamed event carries the run's attribution.
7. **Policy denial.** `notify_customer` needs `orders.notify`, which the policy
   does not grant: `PolicyDenied`.
8. **Redaction.** The secret never appears in the replay export, the snapshot,
   or diagnostics.
9. **Replay.** The recording is round-tripped through JSON and replays the tool
   call from records — the live tool never runs.
10. **Hot replacement, pinned runs.** While a run holds the current generation,
    the `orders-db` provider is replaced; the held run keeps its generation id
    and snapshot digest while the new generation publishes.
11. **Diagnostics and attribution.** Snapshot digests, `explain`, pressure, and
    resource counts describe exactly what is running.
12. **Controlled failure.** `flaky-analytics` setup raises: every effect is
    rolled back, the previous generation stays current, and the failed instance
    stays visible as `FAILED`.
13. **Graceful shutdown.** `stop()` returns every resource to baseline
    (`resource_counts()` shows zero instances, leases, and generations).

## Expected output

```text
[1] preview shows two additions and zero mutation
[2] configuration applied; analytics scope narrows tools to lookup_order
[3] ambiguity reported with candidates, resolved by preference
[4] support-agent@1 materialized with its scoped composition
[5] invoke answered on generation gen_0003 (revision 1)
[6] stream produced 2 attributed events
[7] policy denied the ungranted permission
[8] secret absent from replay records, snapshot, and diagnostics
[9] recording replayed 15 records without live calls
[10] provider replaced while the old run stayed pinned to its generation
[11] snapshot 08eba156a7d3 attributed; 1 live generation(s), 4 instance(s)
[12] failed setup rolled back every effect; the previous generation stayed current
[13] graceful shutdown; 4 instance(s) returned to baseline
support desk: every scenario held
```

The output is deterministic (the printed digest is the *semantic* composition
digest), and `tests/integration/test_reference_app.py` runs the application in
CI and asserts exactly these lines.
