"""Generation pressure under concurrency.

Diagnostics must be *safe* and *cheap* while the control plane transitions
generations and runs hold and release leases concurrently. Reading the report takes
no control-plane lock and performs no ``await``, so it observes a coherent
synchronous snapshot rather than blocking the run path.
"""

from __future__ import annotations

import asyncio

from chassis import Harness, PluginContext, plugin
from chassis.capabilities import CapabilityKey

DATABASE = CapabilityKey("database", "1")

RUNNERS = 12
REPLACEMENTS = 10


def tracked_plugin(name: str):  # type: ignore[no-untyped-def]
    @plugin(name=name, version="1.0.0", provides={"database": "1.0.0"})
    async def provider(ctx: PluginContext) -> None:
        ctx.capabilities.provide(DATABASE, {"name": name})

    return provider


async def wait_until(predicate) -> None:  # type: ignore[no-untyped-def]
    """Yield to the loop until ``predicate`` holds, without sleeping on the clock."""

    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


async def test_concurrent_lease_release_keeps_pressure_authoritative() -> None:
    harness = Harness()
    harness.install(tracked_plugin("provider-a"), entry_id="db")
    await harness.start()
    try:
        entered = [asyncio.Event() for _ in range(RUNNERS)]
        release = [asyncio.Event() for _ in range(RUNNERS)]

        async def run(index: int) -> None:
            async with harness.acquire():
                entered[index].set()
                await release[index].wait()

        tasks = [asyncio.create_task(run(index)) for index in range(RUNNERS)]
        await asyncio.gather(*(event.wait() for event in entered))

        report = harness.diagnostics.generation_pressure()
        assert report.total_leases == RUNNERS
        assert report.generations[0].leases == RUNNERS
        assert report.oldest_lease_age_seconds is not None

        # Release half of the leases concurrently; the count must stay exact.
        for index in range(0, RUNNERS, 2):
            release[index].set()
        await asyncio.gather(*(tasks[index] for index in range(0, RUNNERS, 2)))

        assert harness.diagnostics.generation_pressure().total_leases == RUNNERS // 2

        for index in range(1, RUNNERS, 2):
            release[index].set()
        await asyncio.gather(*tasks)

        settled = harness.diagnostics.generation_pressure()
        assert settled.total_leases == 0
        assert settled.oldest_lease_age_seconds is None
        assert all(entry.leases == 0 for entry in settled.generations)
    finally:
        await harness.stop()


async def test_pressure_is_consistent_while_generations_transition() -> None:
    harness = Harness()
    harness.install(tracked_plugin("provider-0"), entry_id="db")
    await harness.start()

    stop = asyncio.Event()
    failures: list[BaseException] = []

    async def reader() -> None:
        try:
            while not stop.is_set():
                report = harness.diagnostics.generation_pressure()
                entries = {entry.generation_id: entry for entry in report.generations}

                assert report.live_generations == len(report.generations)
                assert report.draining_generations == sum(
                    1 for entry in report.generations if entry.state == "draining"
                )
                assert report.total_leases == sum(entry.leases for entry in report.generations)
                assert all(entry.age_seconds >= 0 for entry in report.generations)
                if report.current_generation_id is not None:
                    assert entries[report.current_generation_id].is_current is True
                for ids in report.instance_generations.values():
                    assert all(generation_id in entries for generation_id in ids)
                await asyncio.sleep(0)
        except BaseException as error:  # pragma: no cover - failure path
            failures.append(error)

    async def run() -> None:
        async with harness.acquire():
            await asyncio.sleep(0)

    readers = [asyncio.create_task(reader()) for _ in range(4)]
    runners = [asyncio.create_task(run()) for _ in range(RUNNERS)]

    try:
        for revision in range(1, REPLACEMENTS + 1):
            harness.install(tracked_plugin(f"provider-{revision}"), entry_id="db", replace=True)
            await harness.reconcile()
            await asyncio.sleep(0)
    finally:
        stop.set()
        await asyncio.gather(*readers, *runners)

    assert failures == []
    assert harness.generation_manager.draining() == ()
    await harness.stop()


async def test_pressure_reports_draining_state_during_shutdown() -> None:
    harness = Harness(name="pressure-shutdown", shutdown_grace_seconds=5.0)
    harness.install(tracked_plugin("provider-a"), entry_id="db")
    await harness.start()

    entered = asyncio.Event()
    release = asyncio.Event()

    async def run() -> None:
        async with harness.acquire():
            entered.set()
            await release.wait()

    runner = asyncio.create_task(run())
    await entered.wait()

    stopping = asyncio.create_task(harness.stop())
    await wait_until(lambda: harness.current_generation is None)

    report = harness.diagnostics.generation_pressure()
    assert report.current_generation_id is None
    assert report.live_generations == 1
    assert report.draining_generations == 1
    assert report.total_leases == 1
    assert report.generations[0].oldest_lease_age_seconds is not None

    release.set()
    await runner
    await stopping
