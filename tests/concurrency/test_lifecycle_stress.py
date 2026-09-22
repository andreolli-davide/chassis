"""Bounded deterministic lifecycle stress with real asyncio tasks (roadmap R029).

These scenarios are the fast, always-on half of the stress coverage: fixed
seeds, bounded iterations, real tasks and real cancellation — never mocked
state transitions. Every scenario ends by proving resources returned to
baseline through `Diagnostics.resource_counts()`. The long-running half lives in
`scripts/soak.py`, opt-in and excluded from the default suite.

Invariants under stress: no lease underflow, no early disposal of reachable
instances, no leaked scope ownership, no hidden background tasks, no false
clean-shutdown report while work remains, deterministic terminal states.
"""

from __future__ import annotations

import asyncio
import contextlib
import random

from chassis import (
    DATABASE,
    MEMORY,
    ChassisError,
    Harness,
    PluginContext,
    plugin,
)
from chassis.testing import TestHarness


@plugin(name="stress-db", version="1.0.0", provides={"database": "1.0.0"})
async def db_plugin(ctx: PluginContext) -> None:
    ctx.capabilities.provide(DATABASE, "db-handle")


@plugin(
    name="stress-memory",
    version="1.0.0",
    provides={"memory": "1.0.0"},
    requires={"database": ">=1,<2"},
)
async def memory_plugin(ctx: PluginContext) -> None:
    ctx.require("database")
    ctx.capabilities.provide(MEMORY, "memory-handle")


def assert_resources_at_baseline(harness: Harness) -> None:
    """Every resource a lifecycle can leak must be gone once the harness stops."""

    counts = harness.diagnostics.resource_counts()
    payload = counts.to_dict()
    assert payload["instances"] == 0, payload
    assert payload["effects"] == 0, payload
    assert payload["tasks"] == 0, payload
    assert payload["stragglers"] == 0, payload
    assert payload["leases"] == 0, payload
    assert payload["live_generations"] == 0, payload
    assert payload["draining_generations"] == 0, payload


async def test_random_cancellation_across_the_lifecycle_returns_to_baseline() -> None:
    """Cancel start/reconcile/run work at unpredictable points, then shut down."""

    rng = random.Random(20260922)

    for _cycle in range(12):
        harness = Harness(shutdown_grace_seconds=5.0)
        harness.install(db_plugin, entry_id="db")
        harness.install(memory_plugin, entry_id="memory")

        operation = asyncio.create_task(harness.start())
        await asyncio.sleep(rng.random() * 0.002)
        if rng.random() < 0.5:
            operation.cancel()
        with contextlib.suppress(asyncio.CancelledError, ChassisError):
            await operation

        await asyncio.wait_for(harness.stop(), timeout=10)
        assert_resources_at_baseline(harness)


async def test_replacement_under_lease_keeps_old_runs_pinned() -> None:
    """A replacement storm must never change or dispose what a held run resolved."""

    harness = TestHarness()
    for index in range(3):
        harness.install(db_plugin, entry_id=f"db-{index}")
    await harness.start()

    holders_ready = asyncio.Event()
    release = asyncio.Event()
    acquired = 0
    pinned: dict[int, tuple[str, str]] = {}
    holder_count = 4

    async def holder(index: int) -> None:
        nonlocal acquired
        async with harness.acquire() as generation:
            digest = harness.snapshot_for(generation).digest()
            pinned[index] = (generation.generation_id, digest)
            acquired += 1
            if acquired == holder_count:
                holders_ready.set()
            await release.wait()
            # Whatever was published meanwhile, this run's world is unchanged.
            assert harness.snapshot_for(generation).digest() == digest
            assert generation.generation_id == pinned[index][0]

    holders = [asyncio.create_task(holder(index)) for index in range(holder_count)]
    await asyncio.wait_for(holders_ready.wait(), timeout=5)

    rng = random.Random(1977)
    for step in range(10):
        entry_id = f"db-{step % 3}"
        harness.install(db_plugin, entry_id=entry_id, config={"generation": step}, replace=True)
        if rng.random() < 0.5:
            await asyncio.sleep(0)
        result = await harness.reconcile()
        assert result is not None and result.generation_id

    pressure = harness.diagnostics.generation_pressure()
    assert pressure.total_leases == holder_count
    assert pressure.draining_generations >= 1

    release.set()
    await asyncio.gather(*holders)
    await asyncio.wait_for(harness.stop(), timeout=10)
    assert_resources_at_baseline(harness)


async def test_repeated_start_stop_cycles_are_deterministic() -> None:
    """Fresh harnesses, same lifecycle, again and again: terminal and leak-free."""

    for _cycle in range(8):
        harness = Harness()
        harness.install(db_plugin, entry_id="db")
        await harness.start()
        generation = harness.current_generation
        assert generation is not None
        async with harness.acquire():
            pass
        await harness.stop()

        assert harness.state.value == "stopped"
        assert_resources_at_baseline(harness)
        counts = harness.diagnostics.resource_counts().to_dict()
        assert counts["entries"] == 1, counts


async def test_shutdown_races_with_runs_and_replacements() -> None:
    """stop() landing mid-flight must still reach a clean terminal state."""

    rng = random.Random(4242)

    for _cycle in range(8):
        harness = TestHarness()
        harness.install(db_plugin, entry_id="db")
        await harness.start()

        async def churn(target: Harness = harness) -> None:
            for step in range(3):
                target.install(db_plugin, entry_id="db", config={"generation": step}, replace=True)
                try:
                    await target.reconcile()
                except ChassisError:
                    return
                await asyncio.sleep(0)

        async def short_run(target: Harness = harness) -> None:
            try:
                async with target.acquire():
                    await asyncio.sleep(rng.random() * 0.002)
            except ChassisError:
                return

        workers = [asyncio.create_task(churn())] + [
            asyncio.create_task(short_run()) for _ in range(3)
        ]
        await asyncio.sleep(rng.random() * 0.002)
        stopping = asyncio.create_task(harness.stop())

        results = await asyncio.gather(*workers, return_exceptions=True)
        for result in results:
            assert result is None or isinstance(result, (ChassisError, asyncio.CancelledError))
        await asyncio.wait_for(stopping, timeout=10)

        assert harness.state.value == "stopped"
        assert_resources_at_baseline(harness)


async def test_large_generation_history_stays_bounded_under_lease() -> None:
    """Retention is bounded, but a leased generation is never evicted or disposed."""

    harness = Harness(generation_history_limit=3)
    harness.install(db_plugin, entry_id="db")
    await harness.start()

    async with harness.acquire() as first:
        first_id = first.generation_id
        first_instance = first.instances[0].instance_id
        for step in range(30):
            harness.install(db_plugin, entry_id="db", config={"generation": step}, replace=True)
            await harness.reconcile()

        pressure = harness.diagnostics.generation_pressure()
        assert pressure.history_limit == 3
        assert pressure.history_retained <= 3
        assert pressure.live_generations >= 2

        # The leased generation is the oldest and its instance must still be
        # reachable through it: bounded history evicts only retired generations.
        reaching = harness.diagnostics.instance_generations(first_instance)
        assert first_id in reaching

    await asyncio.wait_for(harness.stop(), timeout=10)
    assert_resources_at_baseline(harness)


async def test_stress_runs_never_observe_a_disposed_provider() -> None:
    """Held runs keep resolving their provider while replacements churn."""

    harness = TestHarness()
    harness.install(db_plugin, entry_id="db")
    harness.install(memory_plugin, entry_id="memory")
    await harness.start()

    release = asyncio.Event()
    resolved: list[object] = []
    ready = asyncio.Event()
    acquired = 0

    async def consumer_run() -> None:
        nonlocal acquired
        async with harness.acquire() as generation:
            snapshot = harness.snapshot_for(generation)
            assert snapshot.capabilities.get("database"), "provider vanished under a held run"
            resolved.append(snapshot.semantic_digest())
            acquired += 1
            if acquired == 3:
                ready.set()
            await release.wait()
            assert snapshot.capabilities.get("database")

    runs = [asyncio.create_task(consumer_run()) for _ in range(3)]
    await asyncio.wait_for(ready.wait(), timeout=5)

    for step in range(6):
        harness.install(db_plugin, entry_id="db", config={"generation": step}, replace=True)
        await harness.reconcile()

    release.set()
    await asyncio.gather(*runs)
    assert len(set(resolved)) >= 1
    await asyncio.wait_for(harness.stop(), timeout=10)
    assert_resources_at_baseline(harness)
