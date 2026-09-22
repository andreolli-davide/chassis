"""Reproducible performance baselines for Chassis (roadmap R032).

Measures the control-plane and data-plane operations that define capacity —
reconciliation, replacement and cascades, snapshots and hashing, planning and
diagnostics, tool lookup, generation acquire/release, replay lookup, graph-cache
lookup, and configurations at 10 / 100 / 1000 objects — and reports median and
p95 latency with peak memory, captured alongside the exact environment.

Wall-clock numbers are machine-specific: this harness makes no timing claims in
the test suite (the deterministic structural tests in
``tests/perf/test_scaling_structure.py`` gate CI). Use it locally or in a
dedicated job::

    uv run python scripts/benchmark.py
    uv run python scripts/benchmark.py --objects 10,100 --iterations 20
    uv run python scripts/benchmark.py --json benchmarks/baseline-0.9.0.json
    uv run python scripts/benchmark.py --check benchmarks/baseline-0.9.0.json

``--check`` applies conservative regression thresholds (3x the recorded median)
only to scenarios marked ``"stable": true`` in the baseline, and exits non-zero
on a regression. Scenarios that need an optional extra skip cleanly when it is
not installed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import sys
import time
import tracemalloc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from chassis import DATABASE, MEMORY, Harness, PluginContext, plugin
from chassis.persistence.snapshots import chassis_version
from chassis.replay import BoundaryKind, ReplayMode, ReplaySession, boundary_key
from chassis.testing import TestHarness, fake_tool

FORMAT_VERSION = 1
REGRESSION_FACTOR = 3.0


@plugin(name="bench-db", version="1.0.0", provides={"database": "1.0.0"})
async def db_plugin(ctx: PluginContext) -> None:
    ctx.capabilities.provide(DATABASE, f"{ctx.entry_id}-handle")


@plugin(
    name="bench-memory",
    version="1.0.0",
    provides={"memory": "1.0.0"},
    requires={"database": ">=1,<2"},
)
async def memory_plugin(ctx: PluginContext) -> None:
    ctx.require("database")
    ctx.capabilities.provide(MEMORY, f"{ctx.entry_id}-handle")


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    name: str
    objects: int
    iterations: int
    median_ms: float
    p95_ms: float
    peak_bytes: int
    stable: bool
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "objects": self.objects,
            "iterations": self.iterations,
            "median_ms": round(self.median_ms, 4),
            "p95_ms": round(self.p95_ms, 4),
            "peak_bytes": self.peak_bytes,
            "stable": self.stable,
            "note": self.note,
        }


@dataclass(slots=True)
class Workload:
    """A prepared harness and the one operation each iteration times."""

    harness: Harness
    operation: Callable[[int], Awaitable[None]]
    closer: Callable[[], Awaitable[None]] | None = None

    async def close(self) -> None:
        if self.closer is not None:
            await self.closer()
        else:
            await self.harness.stop()


@dataclass(slots=True)
class BenchCase:
    name: str
    stable: bool
    setup: Callable[[int], Awaitable[Workload | None]]
    note: str = ""


async def populated(objects: int) -> Workload:
    """A harness with ``objects`` entries: half providers, half consumers."""

    harness = Harness()
    providers = max(1, objects // 2)
    for index in range(providers):
        harness.install(db_plugin, entry_id=f"db-{index}", config={"pool": index})
    for index in range(objects - providers):
        harness.install(memory_plugin, entry_id=f"memory-{index}")
    await harness.start()

    async def operation(_iteration: int) -> None:
        await harness.reconcile()

    return Workload(harness, operation)


async def replacement(objects: int) -> Workload:
    workload = await populated(objects)
    harness = workload.harness

    async def operation(iteration: int) -> None:
        harness.install(db_plugin, entry_id="db-0", config={"pool": iteration}, replace=True)
        await harness.reconcile()

    return Workload(harness, operation)


async def cascade(objects: int) -> Workload:
    """One provider change that rebuilds the consumer that reaches it."""

    workload = await populated(objects)
    harness = workload.harness

    async def operation(iteration: int) -> None:
        harness.install(db_plugin, entry_id="db-0", config={"pool": iteration}, replace=True)
        await harness.reconcile()

    return Workload(harness, operation)


async def snapshot_hashing(objects: int) -> Workload:
    workload = await populated(objects)
    harness = workload.harness
    generation = harness.current_generation
    assert generation is not None

    async def operation(_iteration: int) -> None:
        snapshot = harness.snapshot_for(generation)
        snapshot.digest()
        snapshot.semantic_digest()

    return Workload(harness, operation)


async def planning_and_diagnostics(objects: int) -> Workload:
    workload = await populated(objects)
    harness = workload.harness

    async def operation(_iteration: int) -> None:
        harness.plan()
        harness.preview().to_dict()
        harness.diagnostics.status()
        harness.diagnostics.generation_pressure()

    return Workload(harness, operation)


async def tool_lookup(objects: int) -> Workload:
    harness = TestHarness()
    harness.install_tools(
        *(
            fake_tool(f"bench-tool-{index}", result="ok", parameters={"x": (str, ...)})
            for index in range(objects)
        )
    )
    await harness.reconcile()
    generation = harness.current_generation
    assert generation is not None

    async def operation(_iteration: int) -> None:
        harness.tool_snapshot(generation)

    return Workload(harness, operation)


async def acquire_release(objects: int) -> Workload:
    workload = await populated(objects)
    harness = workload.harness

    async def operation(_iteration: int) -> None:
        async with harness.acquire():
            pass

    return Workload(harness, operation)


async def replay_lookup(objects: int) -> Workload:
    session = ReplaySession(mode=ReplayMode.RECORD)
    key = boundary_key("tool", "bench", {"x": 1})
    for index in range(objects):
        session.record(
            BoundaryKind.TOOL,
            key=key,
            request={"tool": "bench", "args": {"x": 1}},
            response={"name": "bench", "status": "ok", "content": index},
        )
    replaying = ReplaySession.from_dict(session.to_dict(), mode=ReplayMode.REPLAY)
    harness = Harness(replay=replaying)

    async def operation(_iteration: int) -> None:
        if replaying.has_remaining(BoundaryKind.TOOL, key=key):
            replaying.replay(BoundaryKind.TOOL, key=key)
        else:
            replaying.peek(BoundaryKind.TOOL, key=key)

    return Workload(harness, operation)


async def concurrent_holds(objects: int) -> Workload:
    """Replacement latency while runs pin older generations."""

    workload = await populated(objects)
    harness = workload.harness
    release = asyncio.Event()
    ready = asyncio.Event()
    holders = max(2, min(4, max(1, objects // 4)))
    acquired = 0

    async def hold() -> None:
        nonlocal acquired
        async with harness.acquire():
            acquired += 1
            if acquired == holders:
                ready.set()
            await release.wait()

    tasks = [asyncio.create_task(hold()) for _ in range(holders)]
    await ready.wait()

    async def operation(iteration: int) -> None:
        harness.install(db_plugin, entry_id="db-0", config={"pool": iteration}, replace=True)
        await harness.reconcile()

    async def closer() -> None:
        release.set()
        await asyncio.gather(*tasks)
        await harness.stop()

    return Workload(harness, operation, closer)


async def graph_cache_lookup(objects: int) -> Workload | None:
    """Graph-cache lookup; needs the optional langgraph extra to import."""

    try:
        from chassis.langgraph.graphs import GraphCache, GraphCacheKey
    except ImportError:
        return None

    cache: Any = GraphCache()
    keys = [
        GraphCacheKey(
            agent=f"bench-{index}",
            definition_version="1",
            state_schema_hash=f"state-{index}",
            tool_schema_hash="tools",
            middleware_hash="none",
        )
        for index in range(max(1, objects))
    ]
    for index, key in enumerate(keys):
        cache.put(key, f"compiled-{index}")
    harness = Harness()

    async def operation(iteration: int) -> None:
        cache.get(keys[iteration % len(keys)])

    return Workload(harness, operation)


CASES: tuple[BenchCase, ...] = (
    BenchCase("noop_reconcile", True, populated, "no-op reconciliation of N entries"),
    BenchCase("one_entry_replacement", True, replacement, "replace one entry at N entries"),
    BenchCase("dependency_cascade", True, cascade, "provider change rebuilding its consumer"),
    BenchCase("snapshot_hashing", True, snapshot_hashing, "snapshot construction and hashing"),
    BenchCase(
        "plan_and_diagnostics",
        True,
        planning_and_diagnostics,
        "plan, preview, status, and pressure generation",
    ),
    BenchCase("tool_snapshot_lookup", True, tool_lookup, "tool snapshot over N tools"),
    BenchCase("acquire_release", True, acquire_release, "generation lease round-trip"),
    BenchCase("replay_lookup", True, replay_lookup, "repeated-key replay consumption"),
    BenchCase("graph_cache_lookup", True, graph_cache_lookup, "graph-cache lookup at N entries"),
    BenchCase(
        "concurrent_holds",
        False,
        concurrent_holds,
        "replacement while runs pin old generations",
    ),
)


async def measure(
    case: BenchCase, objects: int, iterations: int, warmup: int
) -> ScenarioResult | None:
    workload = await case.setup(objects)
    if workload is None:
        return None
    try:
        for index in range(warmup):
            await workload.operation(index)
        # Keep a slow case inside a sane time budget: adapt the iteration count
        # to the observed cost of one operation, and report what was actually run.
        probe = time.perf_counter()
        await workload.operation(0)
        probe_ms = max((time.perf_counter() - probe) * 1000.0, 0.001)
        effective = max(3, min(iterations, int(3000.0 / probe_ms)))
        samples: list[float] = []
        peak = 0
        tracemalloc.start()
        try:
            for index in range(effective):
                started = time.perf_counter()
                await workload.operation(index)
                samples.append((time.perf_counter() - started) * 1000.0)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    finally:
        await workload.close()

    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return ScenarioResult(
        name=case.name,
        objects=objects,
        iterations=effective,
        median_ms=statistics.median(samples),
        p95_ms=p95,
        peak_bytes=peak,
        stable=case.stable,
        note=case.note,
    )


def _print(result: ScenarioResult) -> None:
    print(
        f"{result.name:<24} objects={result.objects:>5} "
        f"median={result.median_ms:>9.3f}ms p95={result.p95_ms:>9.3f}ms "
        f"peak={result.peak_bytes:>9}B"
    )


async def run_suite(
    iterations: int, object_counts: list[int], warmup: int, pattern: str | None
) -> list[ScenarioResult]:
    results: list[ScenarioResult] = []
    for case in CASES:
        if pattern and pattern not in case.name:
            continue
        for objects in object_counts:
            result = await measure(case, objects, iterations, warmup)
            if result is None:
                print(f"{case.name}: skipped (an optional extra is not installed)")
                break
            results.append(result)
            _print(result)
    return results


def report_document(results: list[ScenarioResult]) -> dict[str, Any]:
    return {
        "format_version": FORMAT_VERSION,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "chassis": chassis_version(),
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "results": [
            result.to_dict()
            for result in sorted(results, key=lambda item: (item.name, item.objects))
        ],
    }


def check_baseline(document: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    recorded = {(entry["name"], entry["objects"]): entry for entry in baseline.get("results", [])}
    regressions: list[str] = []
    for result in document["results"]:
        if not result["stable"]:
            continue
        previous = recorded.get((result["name"], result["objects"]))
        if previous is None:
            continue
        limit = previous["median_ms"] * REGRESSION_FACTOR
        if result["median_ms"] > limit:
            regressions.append(
                f"{result['name']}@{result['objects']}: median {result['median_ms']:.3f}ms "
                f"> {REGRESSION_FACTOR}x baseline {previous['median_ms']:.3f}ms"
            )
    return regressions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=25, help="timed iterations per case")
    parser.add_argument("--warmup", type=int, default=3, help="untimed warmup iterations")
    parser.add_argument("--objects", type=str, default="10,100,1000", help="object counts per case")
    parser.add_argument("--filter", type=str, default=None, help="only cases with this name")
    parser.add_argument("--json", type=str, default=None, help="write the full document here")
    parser.add_argument("--check", type=str, default=None, help="regression-check a baseline")
    arguments = parser.parse_args(argv)

    object_counts = [int(item) for item in arguments.objects.split(",") if item]
    results = asyncio.run(
        run_suite(arguments.iterations, object_counts, arguments.warmup, arguments.filter)
    )
    document = report_document(results)
    if arguments.json:
        with open(arguments.json, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
        print(f"wrote {arguments.json}")

    if arguments.check:
        with open(arguments.check, encoding="utf-8") as handle:
            baseline = json.load(handle)
        regressions = check_baseline(document, baseline)
        for line in regressions:
            print(f"REGRESSION: {line}", file=sys.stderr)
        if regressions:
            return 1
        print(f"no regression against {arguments.check}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
