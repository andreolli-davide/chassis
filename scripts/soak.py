"""Opt-in long-running lifecycle soak for Chassis (roadmap R029).

The always-on stress suite (`tests/concurrency/test_lifecycle_stress.py`) is
bounded and deterministic. This script runs the same class of scenarios —
cancellation storms, replacement storms under lease, shutdown races, repeated
cycles — for a wall-clock budget with real asyncio tasks, asserting the same
invariants and proving every cycle returns resources to baseline through
`Diagnostics.resource_counts()`.

Excluded from the default fast suite by design. Run it locally or in a
dedicated job::

    uv run python scripts/soak.py --seconds 120
    uv run python scripts/soak.py --cycles 200 --seed 7

It is not a benchmark: it makes no timing claims and reports only pass/fail
counts. Exit code 0 means every cycle upheld every invariant.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import random
import sys
import time

from chassis import (
    DATABASE,
    MEMORY,
    ChassisError,
    Harness,
    HarnessState,
    PluginContext,
    plugin,
)
from chassis.testing import TestHarness


@plugin(name="soak-db", version="1.0.0", provides={"database": "1.0.0"})
async def db_plugin(ctx: PluginContext) -> None:
    ctx.capabilities.provide(DATABASE, "db-handle")


@plugin(
    name="soak-memory",
    version="1.0.0",
    provides={"memory": "1.0.0"},
    requires={"database": ">=1,<2"},
)
async def memory_plugin(ctx: PluginContext) -> None:
    ctx.require("database")
    ctx.capabilities.provide(MEMORY, "memory-handle")


class SoakFailure(AssertionError):
    """An invariant was violated during the soak."""


def check_at_baseline(harness: Harness, cycle: int) -> None:
    counts = harness.diagnostics.resource_counts().to_dict()
    for name in (
        "instances",
        "effects",
        "tasks",
        "stragglers",
        "leases",
        "live_generations",
        "draining_generations",
    ):
        if counts[name] != 0:
            raise SoakFailure(f"cycle {cycle}: {name}={counts[name]} after stop ({counts})")
    if harness.state is not HarnessState.STOPPED:
        raise SoakFailure(f"cycle {cycle}: terminal state is {harness.state.value}")


async def cancellation_cycle(rng: random.Random, cycle: int) -> None:
    harness = Harness(shutdown_grace_seconds=5.0)
    harness.install(db_plugin, entry_id="db")
    harness.install(memory_plugin, entry_id="memory")
    operation = asyncio.create_task(harness.start())
    await asyncio.sleep(rng.random() * 0.002)
    if rng.random() < 0.5:
        operation.cancel()
    with contextlib.suppress(asyncio.CancelledError, ChassisError):
        await operation
    await asyncio.wait_for(harness.stop(), timeout=30)
    check_at_baseline(harness, cycle)


async def replacement_cycle(rng: random.Random, cycle: int) -> None:
    harness = TestHarness()
    harness.install(db_plugin, entry_id="db-a")
    harness.install(db_plugin, entry_id="db-b")
    await harness.start()

    release = asyncio.Event()
    ready = asyncio.Event()
    acquired = 0
    holder_count = 3

    async def holder(index: int) -> None:
        nonlocal acquired
        async with harness.acquire() as generation:
            digest = harness.snapshot_for(generation).digest()
            acquired += 1
            if acquired == holder_count:
                ready.set()
            await release.wait()
            if harness.snapshot_for(generation).digest() != digest:
                raise SoakFailure(f"cycle {cycle}: run {index} observed a changed generation")

    holders = [asyncio.create_task(holder(index)) for index in range(holder_count)]
    await asyncio.wait_for(ready.wait(), timeout=30)

    for step in range(6):
        entry_id = "db-a" if step % 2 else "db-b"
        harness.install(db_plugin, entry_id=entry_id, config={"generation": step}, replace=True)
        if rng.random() < 0.5:
            await asyncio.sleep(0)
        await harness.reconcile()

    release.set()
    await asyncio.gather(*holders)
    await asyncio.wait_for(harness.stop(), timeout=30)
    check_at_baseline(harness, cycle)


async def shutdown_race_cycle(rng: random.Random, cycle: int) -> None:
    harness = TestHarness()
    harness.install(db_plugin, entry_id="db")
    await harness.start()

    async def churn(target: Harness) -> None:
        with contextlib.suppress(ChassisError):
            for step in range(3):
                target.install(db_plugin, entry_id="db", config={"generation": step}, replace=True)
                await target.reconcile()
                await asyncio.sleep(0)

    async def short_run(target: Harness) -> None:
        with contextlib.suppress(ChassisError):
            async with target.acquire():
                await asyncio.sleep(rng.random() * 0.002)

    workers = [asyncio.create_task(churn(harness))] + [
        asyncio.create_task(short_run(harness)) for _ in range(3)
    ]
    await asyncio.sleep(rng.random() * 0.002)
    stopping = asyncio.create_task(harness.stop())
    await asyncio.gather(*workers, return_exceptions=True)
    await asyncio.wait_for(stopping, timeout=30)
    check_at_baseline(harness, cycle)


SCENARIOS = (cancellation_cycle, replacement_cycle, shutdown_race_cycle)


async def soak(seconds: float, cycles: int, seed: int) -> int:
    rng = random.Random(seed)
    deadline = time.perf_counter() + seconds
    completed = 0
    started = time.perf_counter()
    while True:
        if cycles and completed >= cycles:
            break
        if not cycles and time.perf_counter() >= deadline:
            break
        scenario = SCENARIOS[completed % len(SCENARIOS)]
        await scenario(rng, completed)
        completed += 1
        if completed % 25 == 0:
            print(f"  {completed} cycles clean ({time.perf_counter() - started:.1f}s)")
    elapsed = time.perf_counter() - started
    print(f"soak: {completed} cycles, {elapsed:.1f}s, seed {seed} — all invariants held")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=30.0, help="wall-clock budget")
    parser.add_argument("--cycles", type=int, default=0, help="cycle count instead of a budget")
    parser.add_argument("--seed", type=int, default=20260922, help="scheduling seed")
    arguments = parser.parse_args(argv)
    try:
        return asyncio.run(soak(arguments.seconds, arguments.cycles, arguments.seed))
    except SoakFailure as failure:
        print(f"SOAK FAILURE: {failure}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
