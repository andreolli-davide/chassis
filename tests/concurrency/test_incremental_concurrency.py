"""Concurrency around incremental reuse and shared reachability.

Reuse introduces no new lock: the data plane still leases the current generation
without the composition lock, and disposal still waits for every live generation
that can reach a resource. These tests exercise the share/retire/dispose windows
with asyncio events rather than sleeps.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from tests.composition.support import failing, mounted, tracked_provider

from chassis import Harness, HarnessState
from chassis.core.errors import PluginSetupError
from chassis.plugins.lifecycle import PluginState


async def wait_until(predicate: Callable[[], bool]) -> None:
    """Yield to the loop until ``predicate`` holds, bounded to avoid a hang."""

    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


class Holder:
    """Holds one lease on the current generation until released."""

    def __init__(self, harness: Harness) -> None:
        self._harness = harness
        self._entered = asyncio.Event()
        self._release = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def started(self) -> None:
        await self._entered.wait()

    def finish(self) -> None:
        self._release.set()

    async def wait(self) -> None:
        assert self._task is not None
        await self._task

    async def _run(self) -> None:
        async with self._harness.acquire():
            self._entered.set()
            await self._release.wait()


async def test_an_old_generation_leases_while_a_new_one_reuses_shared_resources() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db")
    try:
        await harness.start()
        shared = mounted(harness, "db")

        holder = Holder(harness)
        holder.start()
        await holder.started()

        harness.install(tracked_provider("mem", "memory"), entry_id="mem")
        await harness.reconcile()

        assert mounted(harness, "db") is shared
        assert shared.generation_refs == 2
        assert shared.state is PluginState.ACTIVE

        holder.finish()
        await holder.wait()
        assert shared.state is PluginState.ACTIVE
    finally:
        await harness.stop()


async def test_a_resource_stays_reachable_after_the_final_old_lease_is_released() -> None:
    harness = Harness()
    disposals: list[str] = []
    harness.install(tracked_provider("db", "database", disposals=disposals), entry_id="db")
    try:
        await harness.start()
        holder = Holder(harness)
        holder.start()
        await holder.started()

        harness.install(tracked_provider("mem", "memory"), entry_id="mem")
        await harness.reconcile()
        instance = mounted(harness, "db")

        holder.finish()
        await holder.wait()

        # The current generation still reaches the resource, so it is not disposed.
        assert instance.state is PluginState.ACTIVE
        assert disposals == []
    finally:
        await harness.stop()


async def test_disposal_happens_only_after_the_last_reachable_generation_disappears() -> None:
    harness = Harness()
    disposals: list[str] = []
    harness.install(tracked_provider("db", "database", disposals=disposals), entry_id="db")
    try:
        await harness.start()
        instance = mounted(harness, "db")

        first = Holder(harness)
        second = Holder(harness)
        first.start()
        await first.started()
        second.start()
        await second.started()

        harness.uninstall("db")
        await harness.reconcile()
        assert instance.state is PluginState.ACTIVE
        assert disposals == []

        first.finish()
        await first.wait()
        assert instance.state is PluginState.ACTIVE
        assert disposals == []

        second.finish()
        await second.wait()
        await wait_until(lambda: instance.state is PluginState.DISPOSED)
        assert disposals == ["db"]
    finally:
        await harness.stop()


async def test_a_candidate_failure_while_sharing_leaves_the_shared_resource_valid() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db")
    try:
        await harness.start()
        shared = mounted(harness, "db")

        holder = Holder(harness)
        holder.start()
        await holder.started()

        harness.install(failing("broken"), entry_id="broken")
        with pytest.raises(PluginSetupError):
            await harness.reconcile()

        assert mounted(harness, "db") is shared
        assert shared.state is PluginState.ACTIVE
        assert shared.generation_refs == 1

        holder.finish()
        await holder.wait()
        assert shared.state is PluginState.ACTIVE
    finally:
        await harness.stop()


async def test_diagnostics_are_consistent_while_generations_are_published() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db")
    try:
        await harness.start()
        stop = asyncio.Event()

        async def reader() -> None:
            while not stop.is_set():
                report = harness.diagnostics.generation_pressure()
                assert report.live_generations == len(report.generations)
                assert report.draining_generations == sum(
                    1 for entry in report.generations if entry.state == "draining"
                )
                for resource in report.resources:
                    assert resource.generations
                await asyncio.sleep(0)

        readers = [asyncio.create_task(reader()) for _ in range(4)]
        try:
            for index in range(8):
                harness.install(
                    tracked_provider(f"p{index}", "database"), entry_id="p", replace=True
                )
                await harness.reconcile()
                await asyncio.sleep(0)
        finally:
            stop.set()
            await asyncio.gather(*readers)
    finally:
        await harness.stop()


async def test_shutdown_disposes_shared_resources_exactly_once() -> None:
    harness = Harness(shutdown_grace_seconds=5.0)
    disposals: list[str] = []
    harness.install(tracked_provider("db", "database", disposals=disposals), entry_id="db")
    try:
        await harness.start()

        first = Holder(harness)
        first.start()
        await first.started()

        harness.install(tracked_provider("mem", "memory"), entry_id="mem")
        await harness.reconcile()

        second = Holder(harness)
        second.start()
        await second.started()

        stopping = asyncio.create_task(harness.stop())
        await wait_until(lambda: harness.state is HarnessState.STOPPING)

        first.finish()
        await first.wait()
        assert disposals == []

        second.finish()
        await second.wait()
        await stopping

        assert disposals == ["db"]
        assert harness.plugin_registry.instances() == ()
    finally:
        if harness.state is not HarnessState.STOPPED:
            await harness.stop()
