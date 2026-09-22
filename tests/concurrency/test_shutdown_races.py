"""Races between shutdown, reconciliation, and new runs.

These were found by review rather than by a failing test, so they are pinned here:
shutdown must serialize with composition, and it must not be possible to publish a
generation into a harness that is going away.
"""

from __future__ import annotations

import asyncio

import pytest

from chassis import DATABASE, Harness, Plugin, PluginContext, PluginManifest, plugin
from chassis.core.errors import HarnessStateError
from chassis.testing import TestHarness


def slow_mount(release: asyncio.Event, started: asyncio.Event):  # type: ignore[no-untyped-def]
    """Plugin whose setup blocks, holding a reconciliation in flight."""

    @plugin(name="slow-mount", version="1.0.0", provides={"database": "1.0.0"})
    async def slow_mount(ctx: PluginContext) -> None:
        started.set()
        await release.wait()
        ctx.capabilities.provide(DATABASE, "db")

    return slow_mount


class SlowUnload(Plugin):
    """Plugin whose teardown blocks, holding a shutdown in flight."""

    manifest = PluginManifest(name="slow-unload", version="1.0.0", provides={"database": "1.0.0"})

    def __init__(self, release: asyncio.Event, entered: asyncio.Event) -> None:
        super().__init__()
        self._release = release
        self._entered = entered

    async def setup(self, ctx: PluginContext) -> None:
        ctx.capabilities.provide(DATABASE, "db")

    async def teardown(self, ctx: PluginContext) -> None:
        self._entered.set()
        await self._release.wait()


async def test_shutdown_waits_for_an_in_flight_reconcile() -> None:
    release = asyncio.Event()
    mount_started = asyncio.Event()

    harness = Harness(shutdown_grace_seconds=5.0)
    await harness.start()

    # Install after start so this reconciliation is the one that blocks, rather
    # than the startup reconcile.
    harness.install(slow_mount(release, mount_started), entry_id="slow")
    reconciling = asyncio.ensure_future(harness.reconcile())
    await mount_started.wait()

    stopping = asyncio.ensure_future(harness.stop())
    await asyncio.sleep(0)
    assert not stopping.done(), "shutdown must not run while composition is in flight"

    release.set()
    await reconciling
    await stopping

    assert harness.state.value == "stopped"
    assert harness.plugin_registry.instances() == ()


async def test_reconcile_is_refused_while_stopping() -> None:
    release = asyncio.Event()
    entered = asyncio.Event()

    harness = Harness(shutdown_grace_seconds=5.0)
    harness.install(SlowUnload(release, entered), entry_id="slow")
    await harness.start()

    unloading = asyncio.ensure_future(harness.stop())
    await entered.wait()
    assert harness.state.value == "stopping"

    with pytest.raises(HarnessStateError):
        await harness.reconcile()
    with pytest.raises(HarnessStateError):
        await harness.start()

    release.set()
    await unloading

    assert harness.state.value == "stopped"


async def test_new_runs_cannot_start_after_shutdown_begins() -> None:
    release = asyncio.Event()
    entered = asyncio.Event()

    harness = Harness(shutdown_grace_seconds=5.0)
    harness.install(SlowUnload(release, entered), entry_id="slow")
    await harness.start()

    unloading = asyncio.ensure_future(harness.stop())
    await entered.wait()

    with pytest.raises(HarnessStateError):
        async with harness.acquire():
            pass  # pragma: no cover - acquisition must fail first

    release.set()
    await unloading


async def test_concurrent_stop_calls_are_serialized() -> None:
    harness = TestHarness()
    harness.provide(DATABASE, "db")
    await harness.start()

    await asyncio.gather(harness.stop(), harness.stop(), harness.stop())

    assert harness.state.value == "stopped"
    assert harness.plugin_registry.instances() == ()


async def test_run_released_while_stopping_does_not_resurrect_anything() -> None:
    release = asyncio.Event()
    entered = asyncio.Event()
    run_entered = asyncio.Event()
    run_release = asyncio.Event()

    harness = Harness(shutdown_grace_seconds=5.0)
    harness.install(SlowUnload(release, entered), entry_id="slow")
    await harness.start()

    async def run() -> None:
        async with harness.acquire():
            run_entered.set()
            await run_release.wait()

    task = asyncio.create_task(run())
    await run_entered.wait()

    unloading = asyncio.ensure_future(harness.stop())
    await asyncio.sleep(0)
    assert not unloading.done(), "shutdown must wait for the active run"

    run_release.set()
    await task

    # Disposal now runs the plugin's teardown, which the harness awaits.
    assert not unloading.done()
    release.set()
    await unloading

    assert harness.plugin_registry.instances() == ()
    assert harness.capability_registry.registrations() == ()
    assert harness.state.value == "stopped"


async def test_reconcile_queued_behind_shutdown_cannot_publish() -> None:
    release = asyncio.Event()
    mount_started = asyncio.Event()

    harness = Harness(shutdown_grace_seconds=5.0)
    await harness.start()

    # Hold the composition lock with an in-flight reconciliation.
    harness.install(slow_mount(release, mount_started), entry_id="slow")
    composing = asyncio.ensure_future(harness.reconcile())
    await mount_started.wait()

    # Queue a shutdown, then a reconciliation behind it. The lock is FIFO, so the
    # shutdown runs first and the reconciliation wakes up in a stopped harness.
    unloading = asyncio.ensure_future(harness.stop())
    await asyncio.sleep(0)

    harness.install(slow_mount(release, mount_started), entry_id="late")
    late = asyncio.ensure_future(harness.reconcile())
    await asyncio.sleep(0)
    # The refusal is immediate now that stop() flips to STOPPING before its
    # first yield; what matters is below: the late reconcile can never publish.

    release.set()
    await composing
    await unloading

    with pytest.raises(HarnessStateError):
        await late

    # Nothing was mounted into, or published to, the stopped harness.
    assert harness.state.value == "stopped"
    assert harness.current_generation is None
    assert harness.plugin_registry.instances() == ()
