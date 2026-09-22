# Performance and capacity baselines

0.9.0 establishes reproducible measurements before any optimization
(roadmap R032): what is measured, how, at which sizes, and where the known
scaling limits are. Wall-clock numbers are machine-specific — the deterministic
structural tests in `tests/perf/test_scaling_structure.py` (which count work,
not time) are what gate CI.

## Methodology

`scripts/benchmark.py` builds real harnesses with `@plugin` fakes and times one
operation per iteration with `time.perf_counter`, after warmup. Each scenario
reports:

- median and p95 latency over N iterations (N adapts to keep a slow case
  inside a sane time budget and is reported per result);
- the object count the scenario was built at (10 / 100 / 1000 entries, tools,
  or records);
- `tracemalloc` peak bytes across the timed iterations;
- the environment: Python version, platform, machine, chassis version, and
  capture time.

```bash
uv run python scripts/benchmark.py                                    # full run
uv run python scripts/benchmark.py --objects 10,100 --iterations 20   # quick run
uv run python scripts/benchmark.py --json benchmarks/baseline-0.9.0.json
uv run python scripts/benchmark.py --check benchmarks/baseline-0.9.0.json
```

`--check` compares medians against the checked-in baseline and fails only for
scenarios marked `"stable": true`, with a deliberately conservative threshold
(3x the recorded median) so shared-runner noise cannot fail it. The
`concurrent_holds` scenario is recorded but not gated: its timing depends on
scheduling.

## The 0.9.0 baseline

Recorded on Apple M1 (`macOS-26.0-arm64`, Python 3.12.13) with the exact
command above; the full document is
[`benchmarks/baseline-0.9.0.json`](https://github.com/andreolli-davide/chassis/blob/main/benchmarks/baseline-0.9.0.json).
(The environment capture reports the distribution version at capture time,
0.8.1: the version bump itself lands in the final local release-preparation
commit. The measured tree is the 0.9.0 code base.)

Median latency in milliseconds:

| Scenario | 10 objects | 100 objects | 1000 objects | Growth |
| --- | --- | --- | --- | --- |
| `acquire_release` | 0.017 | 0.017 | 0.017 | flat |
| `tool_snapshot_lookup` | 0.011 | 0.036 | 0.255 | linear |
| `replay_lookup` | 0.004 | 0.012 | 0.063 | linear |
| `graph_cache_lookup` | 0.038 | 0.038 | 0.038 | flat |
| `snapshot_hashing` | 2.2 | 7.4 | 59.0 | linear |
| `noop_reconcile` | 6.3 | 173.3 | 13,898.7 | **superlinear** |
| `one_entry_replacement` | 7.0 | 174.5 | 14,730.9 | **superlinear** |
| `dependency_cascade` | 6.6 | 175.2 | 14,836.7 | **superlinear** |
| `concurrent_holds` (not gated) | 6.6 | 178.8 | 15,309.9 | **superlinear** |
| `plan_and_diagnostics` | 14.2 | 462.4 | 39,354.5 | **superlinear** |

Peak allocation for the same scenarios: data-plane lookups stay in the tens of
kilobytes regardless of size; a 1000-entry reconcile peaks around 130–205 MB.

## Expected capacity and known scaling limits

- **The data plane is flat.** Leasing a generation, resolving a tool snapshot,
  consuming a replay record, and a graph-cache lookup cost microseconds and
  scale at most linearly with the number of tools or records. Runs are
  unaffected by control-plane size except through the snapshot they carry
  (snapshot construction and hashing is linear).
- **The control plane is superlinear today.** Reconciliation and planning grow
  roughly quadratically with installed entries: dependency resolution evaluates
  candidate assessments and semantic identities per entry per plan, and the
  resolver's fixpoint re-walks pending work. Measured: a 10-entry composition
  reconciles in ~6 ms, 100 entries in ~175 ms, and 1000 entries in ~14 s.
- **Practical guidance.** Treat **up to ~200 entries per harness** as the
  comfortable control-plane range for interactive reconciliation at 0.9.0
  (a few hundred milliseconds per reconcile). Larger compositions should
  partition across harnesses/scopes or expect multi-second reconciles; the
  data plane keeps serving at full speed while a slow reconcile runs
  (composition is serialized on the control plane only — guarantee G8).
- **Generation retention is bounded.** History is bounded by
  `generation_history_limit`; leases — not history — decide liveness, so a
  loitering run costs memory proportional to the plugins it keeps reachable.
- **Replay storage is linear** in record count; consumption is per boundary
  key.

The superlinear resolver is a measured, documented limit — deliberately **not**
optimized in 0.9.0: this milestone is the compatibility freeze, and the fixpoint
touches load-bearing resolution code (G3, G12, G18). The baseline exists so the
optimization can be verified when it happens; `scripts/benchmark.py --check`
and `tests/perf/test_scaling_structure.py` are its acceptance harness.

## Structural tests

`tests/perf/test_scaling_structure.py` counts work instead of timing it and
runs in the normal suite:

- reconcile and preview compute **exactly one semantic identity per entry**
  (a quadratic implementation would grow with N²);
- one tool-snapshot lookup is **one registry scan**, independent of tool count;
- acquire/release performs **zero composition work** — the data plane never
  recomputes identity per run.
