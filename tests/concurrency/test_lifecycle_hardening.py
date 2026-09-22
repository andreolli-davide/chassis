"""Cancellation, shutdown, and draining hardening (roadmap R029).

Each test pins one gap found by the 0.9.0 lifecycle audit: shutdown as a
barrier for every caller, a cancelled stop still reaching the terminal state,
one grace bound for every draining generation, plugin scopes honoring the
configured task timeout, transactional candidate builds, refused acquisition
while stopping, and a deterministic one-shot lifecycle. Real asyncio tasks
throughout.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from chassis import (
    DATABASE,
    Harness,
    HarnessState,
    Plugin,
    PluginContext,
    PluginManifest,
    plugin,
)
from chassis.capabilities.snapshot import CapabilitySnapshot
from chassis.core.errors import EffectCleanupError, HarnessStateError
from chassis.core.generation import GenerationLease
from chassis.core.generations import GenerationManager
from chassis.core.scope import Scope


def _empty_snapshot(generation_id: str) -> CapabilitySnapshot:
    return CapabilitySnapshot(generation_id=generation_id)


@plugin(name="db", version="1.0.0", provides={"database": "1.0.0"})
async def db_plugin(ctx: PluginContext) -> None:
    ctx.capabilities.provide(DATABASE, "db-handle")


async def test_stop_is_a_barrier_for_every_caller() -> None:
    """No caller may observe a stopped harness while disposal still runs."""

    timeline: list[str] = []
    release = asyncio.Event()
    tearing_down = asyncio.Event()

    class SlowDispose(Plugin):
        manifest = PluginManifest(name="slow-dispose", version="1.0.0")

        async def setup(self, ctx: PluginContext) -> None:
            return None

        async def teardown(self, ctx: PluginContext) -> None:
            timeline.append("teardown-start")
            tearing_down.set()
            await release.wait()
            timeline.append("teardown-end")

    harness = Harness()
    harness.install(SlowDispose(), entry_id="slow-dispose")
    await harness.start()

    first = asyncio.create_task(harness.stop())
    await tearing_down.wait()
    second = asyncio.create_task(harness.stop())
    await asyncio.sleep(0.05)
    assert timeline == ["teardown-start"], "the second caller returned before disposal finished"

    release.set()
    await asyncio.gather(first, second)
    timeline.append("both-returned")

    assert timeline == ["teardown-start", "teardown-end", "both-returned"]
    assert harness.state is HarnessState.STOPPED


async def test_a_cancelled_stop_still_reaches_the_terminal_state() -> None:
    """Cancelling one caller must not wedge the harness in STOPPING."""

    harness = Harness()
    harness.install(db_plugin, entry_id="db")
    await harness.start()

    stopping = asyncio.create_task(harness.stop())
    await asyncio.sleep(0)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping

    # The shutdown kept running in the background; the next stop() is a barrier
    # over the same run and observes the terminal state.
    await asyncio.wait_for(harness.stop(), timeout=5)

    assert harness.state is HarnessState.STOPPED
    counts = harness.diagnostics.resource_counts()
    assert counts.instances == 0
    assert counts.leases == 0
    assert counts.live_generations == 0


async def test_drain_uses_one_deadline_for_every_generation() -> None:
    """The grace bounds the whole wait, not one wait per generation."""

    manager = GenerationManager()
    leases: list[GenerationLease] = []
    for _ in range(4):
        manager.publish(manager.build(snapshot_factory=_empty_snapshot, instances=()))
        leases.append(manager.acquire_lease())
    assert len(manager.draining()) == 3

    started = time.perf_counter()
    idle, busy = await manager.drain(manager.begin_shutdown(), timeout_seconds=0.2)
    elapsed = time.perf_counter() - started

    assert idle == ()
    assert len(busy) == 4
    # Sequential per-generation deadlines would need >= 0.8s here.
    assert elapsed < 0.7, f"drain waited {elapsed:.2f}s for 4 generations on one 0.2s grace"

    for lease in leases:
        manager.release_lease(lease)


async def test_plugin_scopes_honor_the_configured_task_shutdown_timeout() -> None:
    """The harness setting reaches plugin scopes instead of a hardcoded default."""

    scopes: list[Scope] = []
    release = asyncio.Event()
    owned: list[asyncio.Task[None]] = []

    class Stubborn(Plugin):
        manifest = PluginManifest(name="stubborn", version="1.0.0")

        async def setup(self, ctx: PluginContext) -> None:
            scopes.append(ctx.scope)

            async def refuse() -> None:
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue

            owned.append(ctx.create_task(refuse(), name="unkillable"))

        async def teardown(self, ctx: PluginContext) -> None:
            return None

    harness = Harness(task_shutdown_timeout=0.05)
    harness.install(Stubborn(), entry_id="stubborn")
    await harness.start()

    started = time.perf_counter()
    try:
        with pytest.raises(EffectCleanupError):
            await harness.stop()
        elapsed = time.perf_counter() - started

        # The configured 0.05s bound applies; the 5s default would dominate.
        assert elapsed < 2.0, f"stop waited {elapsed:.2f}s on a 0.05s task timeout"
        scope = scopes[0]
        assert scope.stragglers, "the unkillable task must stay visible"
        assert scope.fully_disposed is False
    finally:
        release.set()
        for task in owned:
            await task


async def test_a_failed_candidate_build_rolls_back_every_mounted_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure after setup but before publication leaks no instance."""

    def boom(**_kwargs: object) -> object:
        raise RuntimeError("candidate build failed")

    monkeypatch.setattr("chassis.harness.build_scope_tree", boom)

    harness = Harness()
    harness.install(db_plugin, entry_id="db")
    with pytest.raises(RuntimeError):
        await harness.reconcile()

    counts = harness.diagnostics.resource_counts()
    assert counts.instances == 0, "a mounted instance survived a failed candidate build"
    assert counts.effects == 0
    assert harness.diagnostics.status()["plugins"]["mounted"] == 0
    await harness.stop()


async def test_acquisition_is_refused_while_the_harness_is_stopping() -> None:
    """Shutdown stops accepting new work before it starts draining."""

    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingTeardown(Plugin):
        manifest = PluginManifest(name="blocking", version="1.0.0")

        async def setup(self, ctx: PluginContext) -> None:
            return None

        async def teardown(self, ctx: PluginContext) -> None:
            entered.set()
            await release.wait()

    harness = Harness()
    harness.install(BlockingTeardown(), entry_id="blocking")
    await harness.start()

    stopping = asyncio.create_task(harness.stop())
    await entered.wait()
    assert harness.state is HarnessState.STOPPING

    with pytest.raises(HarnessStateError):
        async with harness.acquire():
            pytest.fail("a run acquired a generation while the harness was stopping")

    release.set()
    await stopping


async def test_a_stopped_harness_refuses_to_start_again() -> None:
    """One-shot lifecycle: the terminal state is explicit and deterministic."""

    harness = Harness()
    harness.install(db_plugin, entry_id="db")
    await harness.start()
    await harness.stop()

    with pytest.raises(HarnessStateError) as excinfo:
        await harness.start()

    assert excinfo.value.context["state"] == "stopped"
    counts = harness.diagnostics.resource_counts()
    assert counts.instances == 0
    assert counts.leases == 0
    assert counts.live_generations == 0


async def test_a_teardown_raising_cancelled_error_still_disposes_every_instance() -> None:
    """A teardown that raises CancelledError is a failing teardown, not an
    abort: every other instance must still be disposed and the harness must
    reach its terminal state with the failure aggregated."""

    torn_down: list[str] = []

    class CancellingTeardown(Plugin):
        manifest = PluginManifest(
            name="cancelling", version="1.0.0", provides={"database": "1.0.0"}
        )

        async def setup(self, ctx: PluginContext) -> None:
            from chassis import DATABASE

            ctx.capabilities.provide(DATABASE, "db-handle")

        async def teardown(self, ctx: PluginContext) -> None:
            torn_down.append("cancelling")
            raise asyncio.CancelledError()

    class QuietTeardown(Plugin):
        manifest = PluginManifest(name="quiet", version="1.0.0")

        async def setup(self, ctx: PluginContext) -> None:
            return None

        async def teardown(self, ctx: PluginContext) -> None:
            torn_down.append("quiet")

    harness = Harness()
    harness.install(CancellingTeardown(), entry_id="cancelling")
    harness.install(QuietTeardown(), entry_id="quiet")
    await harness.start()

    with pytest.raises(EffectCleanupError) as excinfo:
        await asyncio.wait_for(harness.stop(), timeout=10)

    assert sorted(torn_down) == ["cancelling", "quiet"], "the teardown loop was aborted"
    assert harness.state is HarnessState.STOPPED
    counts = harness.diagnostics.resource_counts()
    assert counts.instances == 0, "an instance leaked past the shutdown"
    assert counts.live_generations == 0
    error = excinfo.value
    assert any(isinstance(failure.error, asyncio.CancelledError) for failure in error.failures), (
        "the cancelled teardown must be aggregated, not lost"
    )

    # Terminal state is reachable and idempotent: a second stop observes the
    # same completed shutdown instead of re-raising a cancelled task forever.
    with pytest.raises(EffectCleanupError):
        await harness.stop()
    assert harness.state is HarnessState.STOPPED


async def test_stop_refuses_runs_queued_behind_it() -> None:
    """stop() flips to STOPPING before its first yield: an acquisition task
    already in the ready queue must be refused."""

    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingTeardown(Plugin):
        manifest = PluginManifest(name="blocking-late", version="1.0.0")

        async def setup(self, ctx: PluginContext) -> None:
            return None

        async def teardown(self, ctx: PluginContext) -> None:
            entered.set()
            await release.wait()

    harness = Harness()
    harness.install(BlockingTeardown(), entry_id="blocking")
    await harness.start()

    outcome: list[str] = []

    async def try_acquire() -> None:
        try:
            async with harness.acquire():
                outcome.append("acquired")
        except HarnessStateError:
            outcome.append("refused")

    stopping = asyncio.create_task(harness.stop())
    queued = asyncio.create_task(try_acquire())
    await asyncio.wait_for(queued, timeout=10)

    assert outcome == ["refused"], "a run acquired after stop() began"
    release.set()
    await stopping
