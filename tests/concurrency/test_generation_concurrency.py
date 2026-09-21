"""Concurrency tests that actively try to break generation lifetime invariants.

Each test drives the real harness with many concurrent runs while the control
plane publishes and retires generations, then asserts the invariants that matter:
a run never uses a disposed provider, acquisition never returns a retiring
generation, and a plugin is disposed only when nothing can reach it.
"""

from __future__ import annotations

import asyncio

import pytest

from chassis import Harness, PluginContext, plugin
from chassis.capabilities import CapabilityKey
from chassis.capabilities.snapshot import CapabilitySnapshot
from chassis.core.errors import EffectCleanupError, UnknownLeaseError
from chassis.core.generation import GenerationLease, GenerationState
from chassis.core.generations import GenerationManager
from chassis.core.scope import Scope
from chassis.plugins.lifecycle import PluginInstance, PluginState
from chassis.plugins.manifest import PluginManifest

DATABASE = CapabilityKey("database", "1")

RUNNERS = 8
ITERATIONS = 4
REPLACEMENTS = 12


class TrackedProvider:
    """Provider that fails loudly if it is used after disposal."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.uses = 0
        self.disposed = False

    def use(self) -> None:
        if self.disposed:
            raise AssertionError(f"{self.name} was used after dispose")
        self.uses += 1


def tracked_plugin(name: str, disposals: list[str]):  # type: ignore[no-untyped-def]
    @plugin(name=name, version="1.0.0", provides={"database": "1.0.0"})
    async def provider(ctx: PluginContext) -> None:
        value = TrackedProvider(name)
        ctx.capabilities.provide(DATABASE, value)

        def on_close() -> None:
            value.disposed = True
            disposals.append(name)

        ctx.cleanup(f"{name} disposed", on_close)

    return provider


def mounted(harness: Harness, entry_id: str) -> PluginInstance:
    instance = harness.plugin_registry.instance(entry_id)
    assert instance is not None
    return instance


async def test_runs_never_observe_a_provider_that_was_disposed_under_them() -> None:
    disposals: list[str] = []
    harness = Harness()
    harness.install(tracked_plugin("provider-0", disposals), entry_id="db")
    await harness.start()

    stop = asyncio.Event()
    failures: list[BaseException] = []

    async def run() -> None:
        try:
            while not stop.is_set():
                async with harness.acquire() as generation:
                    provider = generation.snapshot.require(DATABASE)
                    assert isinstance(provider, TrackedProvider)
                    for _ in range(3):
                        provider.use()
                        await asyncio.sleep(0)
        except BaseException as error:
            failures.append(error)

    runners = [asyncio.create_task(run()) for _ in range(RUNNERS)]
    await asyncio.sleep(0)

    try:
        for revision in range(1, REPLACEMENTS + 1):
            harness.install(
                tracked_plugin(f"provider-{revision}", disposals), entry_id="db", replace=True
            )
            await harness.reconcile()
            await asyncio.sleep(0)
    finally:
        stop.set()
        await asyncio.gather(*runners)

    assert failures == []
    # Every superseded provider is disposed exactly once; the timestamps of those
    # disposals depend on when runs released their leases, so only the set is
    # meaningful. The current provider is never disposed.
    assert sorted(disposals) == sorted(f"provider-{revision}" for revision in range(REPLACEMENTS))
    assert len(disposals) == REPLACEMENTS
    assert mounted(harness, "db").manifest.name == f"provider-{REPLACEMENTS}"
    assert harness.plugin_registry.instances() == (mounted(harness, "db"),)
    assert harness.generation_manager.draining() == ()

    await harness.stop()


async def test_acquisition_never_returns_a_retiring_generation() -> None:
    disposals: list[str] = []
    harness = Harness()
    harness.install(tracked_plugin("provider-0", disposals), entry_id="db")
    await harness.start()

    observed: list[str] = []
    failures: list[BaseException] = []

    async def run() -> None:
        try:
            for _ in range(ITERATIONS):
                async with harness.acquire() as generation:
                    # The generation is coherent for the whole run: its provider is
                    # alive before and after yielding to the replacement loop.
                    assert generation.state.value == "active"
                    provider = generation.snapshot.require(DATABASE)
                    assert isinstance(provider, TrackedProvider)
                    provider.use()
                    observed.append(generation.generation_id)
                    await asyncio.sleep(0)
                    provider.use()
        except BaseException as error:
            failures.append(error)

    runners = [asyncio.create_task(run()) for _ in range(RUNNERS)]
    for revision in range(1, REPLACEMENTS + 1):
        harness.install(
            tracked_plugin(f"provider-{revision}", disposals), entry_id="db", replace=True
        )
        await harness.reconcile()
        await asyncio.sleep(0)
    await asyncio.gather(*runners)

    assert failures == []
    assert len(observed) == RUNNERS * ITERATIONS
    assert harness.generation_manager.draining() == ()

    await harness.stop()


async def test_disposal_happens_only_after_every_lease_is_released() -> None:
    disposals: list[str] = []
    harness = Harness()
    harness.install(tracked_plugin("provider-a", disposals), entry_id="db")
    await harness.start()

    entered = [asyncio.Event() for _ in range(16)]
    release = asyncio.Event()

    async def run(index: int) -> None:
        async with harness.acquire():
            entered[index].set()
            await release.wait()

    runners = [asyncio.create_task(run(index)) for index in range(len(entered))]
    await asyncio.gather(*(event.wait() for event in entered))

    instance = mounted(harness, "db")
    harness.uninstall("db")
    await harness.reconcile()

    # Sixteen runs still hold the generation that reaches this instance.
    assert instance.state is PluginState.ACTIVE
    assert disposals == []
    assert instance.generation_refs == 1

    release.set()
    await asyncio.gather(*runners)

    assert disposals == ["provider-a"]
    assert instance.state is PluginState.DISPOSED

    await harness.stop()


async def test_concurrent_reconciles_are_serialized_and_consistent() -> None:
    disposals: list[str] = []
    harness = Harness()
    harness.install(tracked_plugin("provider-0", disposals), entry_id="db")
    await harness.start()

    try:
        for revision in range(1, 6):
            harness.install(
                tracked_plugin(f"provider-{revision}", disposals), entry_id="db", replace=True
            )
            await asyncio.gather(
                harness.reconcile(),
                harness.reconcile(),
                harness.reconcile(),
            )

        # Serialized reconciliation must leave exactly one reachable instance.
        assert harness.plugin_registry.instances() == (mounted(harness, "db"),)
        assert harness.current_generation is not None
        assert harness.current_generation.snapshot.require(DATABASE).name == "provider-5"
        assert harness.generation_manager.draining() == ()
    finally:
        await harness.stop()


async def test_shutdown_with_a_stuck_run_reports_the_timeout_and_still_disposes() -> None:
    harness = Harness(name="stuck", shutdown_grace_seconds=0.05)
    harness.install(tracked_plugin("provider-a", []), entry_id="db")
    await harness.start()

    instance = mounted(harness, "db")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stuck_run() -> None:
        async with harness.acquire():
            entered.set()
            await release.wait()

    task = asyncio.create_task(stuck_run())
    await entered.wait()

    with pytest.raises(EffectCleanupError) as excinfo:
        await harness.stop()

    assert len(excinfo.value.failures) == 1
    assert isinstance(excinfo.value.failures[0].error, TimeoutError)
    assert instance.state is PluginState.DISPOSED
    assert harness.state.value == "stopped"

    release.set()
    await task


async def test_shutdown_waits_for_runs_that_finish_within_the_grace_period() -> None:
    harness = Harness(name="graceful", shutdown_grace_seconds=5.0)
    harness.install(tracked_plugin("provider-a", []), entry_id="db")
    await harness.start()

    instance = mounted(harness, "db")
    finished: list[bool] = []

    async def short_run() -> None:
        async with harness.acquire():
            await asyncio.sleep(0.01)
            finished.append(True)

    task = asyncio.create_task(short_run())
    await asyncio.sleep(0)

    await harness.stop()
    await task

    assert finished == [True]
    assert instance.state is PluginState.DISPOSED


# --------------------------------------------------------------------------
# Lease-accounting regressions (R001): real tasks and event barriers only.
# --------------------------------------------------------------------------


def accounting_instance(name: str) -> PluginInstance:
    """A minimal instance used only for reachability accounting."""

    return PluginInstance(
        instance_id=f"plugin_{name}",
        entry_id=name,
        manifest=PluginManifest(name=name, version="1.0.0"),
        plugin=None,  # type: ignore[arg-type]
        scope=Scope(name),
        state=PluginState.ACTIVE,
    )


def empty_snapshot(generation_id: str) -> CapabilitySnapshot:
    return CapabilitySnapshot(generation_id=generation_id)


async def test_reacquisition_after_idle_does_not_wake_a_new_waiter() -> None:
    """A waiter must join the *current* lease cycle, not the previous idle one."""

    manager = GenerationManager()
    active = manager.build(snapshot_factory=empty_snapshot, instances=[])
    manager.publish(active)

    first = manager.acquire_lease()
    manager.release_lease(first)  # the generation is idle again

    acquired = asyncio.Event()
    release = asyncio.Event()

    async def holder_run() -> GenerationLease:
        lease = manager.acquire_lease()
        acquired.set()
        await release.wait()
        return lease

    hold_task = asyncio.create_task(holder_run())
    await acquired.wait()

    waiter = asyncio.create_task(active.accounting.wait_idle())
    await asyncio.sleep(0)
    assert not waiter.done()

    release.set()
    second = await hold_task
    manager.release_lease(second)
    await waiter


async def test_concurrent_duplicate_releases_count_exactly_once() -> None:
    manager = GenerationManager()
    active = manager.build(snapshot_factory=empty_snapshot, instances=[])
    manager.publish(active)
    lease = manager.acquire_lease()

    start = asyncio.Event()
    rejected: list[BaseException] = []

    async def release_attempt() -> None:
        await start.wait()
        try:
            manager.release_lease(lease)
        except UnknownLeaseError as error:
            rejected.append(error)

    attempts = [asyncio.create_task(release_attempt()) for _ in range(2)]
    await asyncio.sleep(0)
    start.set()
    await asyncio.gather(*attempts)

    # Exactly one release counts; the duplicate is rejected without effect.
    assert len(rejected) == 1
    assert active.lease_count == 0


async def test_shutdown_during_a_live_second_lease_is_reported_and_still_disposes() -> None:
    disposals: list[str] = []
    harness = Harness(name="two-leases", shutdown_grace_seconds=0.05)
    harness.install(tracked_plugin("provider-a", disposals), entry_id="db")
    await harness.start()

    generation = harness.current_generation
    assert generation is not None

    # One run completes first, so the generation has been idle: a reacquired
    # lease must rearm the idle wait rather than inherit the stale idle signal.
    async with harness.acquire():
        await asyncio.sleep(0)

    entered = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()

    async def stuck(index: int) -> None:
        async with harness.acquire():
            entered[index].set()
            await release.wait()

    runs = [asyncio.create_task(stuck(index)) for index in range(2)]
    await asyncio.gather(*(event.wait() for event in entered))
    assert generation.lease_count == 2

    # Shutdown must observe the two live leases, report the timeout, and still
    # dispose at the terminal boundary.
    with pytest.raises(EffectCleanupError) as excinfo:
        await harness.stop()

    assert len(excinfo.value.failures) == 1
    assert isinstance(excinfo.value.failures[0].error, TimeoutError)
    assert generation.state is GenerationState.RETIRED
    assert generation.lease_count == 2
    assert disposals == ["provider-a"]

    # The late releases of both runs stay exact.
    release.set()
    await asyncio.gather(*runs)
    assert generation.lease_count == 0


async def test_a_duplicate_release_cannot_reclaim_a_shared_instance_under_a_live_run() -> None:
    manager = GenerationManager()
    shared = accounting_instance("shared")
    old = manager.build(snapshot_factory=empty_snapshot, instances=[shared])
    manager.publish(old)

    stale = manager.acquire_lease()

    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder_run() -> GenerationLease:
        lease = manager.acquire_lease()
        entered.set()
        await release.wait()
        # The run is still holding the old generation and its shared provider.
        assert "plugin_shared" in manager.reachable_instance_ids()
        return lease

    hold_task = asyncio.create_task(holder_run())
    await entered.wait()

    manager.publish(manager.build(snapshot_factory=empty_snapshot, instances=[shared]))
    assert old.state is GenerationState.DRAINING

    # A buggy caller double-releases; only the first release counts.
    assert manager.release_lease(stale) is False
    with pytest.raises(UnknownLeaseError):
        manager.release_lease(stale)

    # The duplicate must not fire the reclaim signal while the holder's run is
    # still live through the old generation.
    assert old.lease_count == 1

    manager.refresh_references([shared])
    assert shared.generation_refs == 2

    release.set()
    holder = await hold_task
    assert manager.release_lease(holder) is True

    manager.retire(old)
    manager.refresh_references([shared])
    assert shared.generation_refs == 1
