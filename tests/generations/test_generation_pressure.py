"""Generation pressure: authoritative liveness, lease age, and retention.

The guarantee under test is that a run keeps the composition it acquired, and that
operators can *see* when that is holding resources for longer than expected. The
report may never be derived from the bounded diagnostics history: liveness is a
property of the live set, and the history buffer only ever evicts retired
generations.
"""

from __future__ import annotations

import asyncio

from chassis import Harness, PluginContext, plugin
from chassis.capabilities import CapabilityKey
from chassis.capabilities.snapshot import CapabilitySnapshot
from chassis.core.generations import GenerationManager
from chassis.plugins.lifecycle import PluginInstance, PluginState

DATABASE = CapabilityKey("database", "1")
MEMORY = CapabilityKey("memory", "1")


def empty_snapshot(generation_id: str) -> CapabilitySnapshot:
    return CapabilitySnapshot(generation_id=generation_id)


class Guard:
    """Provider payload that complains if it is used after its scope closed."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.disposed = False

    def use(self) -> str:
        if self.disposed:
            raise RuntimeError(f"{self.name} used after dispose")
        return self.name


def guard_plugin(name: str, *, version: str = "1.0.0", capability: CapabilityKey = DATABASE):  # type: ignore[no-untyped-def]
    @plugin(name=name, version=version, provides={capability.name: version})
    async def guard(ctx: PluginContext) -> None:
        value = Guard(name)
        ctx.capabilities.provide(capability, value, version=version)
        ctx.cleanup(f"{name} disposed", lambda: setattr(value, "disposed", True))

    return guard


def mounted(harness: Harness, entry_id: str) -> PluginInstance:
    instance = harness.plugin_registry.instance(entry_id)
    assert instance is not None, f"entry {entry_id!r} is not mounted"
    return instance


class Holder:
    """Holds a lease on the current generation until told to release it."""

    def __init__(self, harness: Harness) -> None:
        self._harness = harness
        self._entered = asyncio.Event()
        self._release = asyncio.Event()
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        async with self._harness.acquire():
            self._entered.set()
            await self._release.wait()

    async def started(self) -> None:
        await self._entered.wait()

    async def finish(self) -> None:
        self._release.set()
        await self._task


# ---------------------------------------------------------------- unit-level


class FakeClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_lease_age_is_authoritative_and_outlives_out_of_order_release() -> None:
    """Age tracks the *actual* oldest outstanding lease, not acquisition order."""

    clock = FakeClock()
    manager = GenerationManager(clock=clock)
    generation = manager.build(snapshot_factory=empty_snapshot, instances=[])
    manager.publish(generation)

    old = manager.acquire_lease()
    clock.advance(100)
    recent = manager.acquire_lease()
    clock.advance(50)

    # The newer lease is released first; the old one is still outstanding.
    manager.release_lease(recent)

    assert generation.lease_count == 1
    assert generation.oldest_lease_age_seconds == 150
    assert old.age_seconds == 150


# ------------------------------------------------------------- harness-level


async def test_one_current_generation_has_no_pressure() -> None:
    harness = Harness()
    harness.install(guard_plugin("provider-a"), entry_id="db")
    await harness.start()
    try:
        generation = harness.current_generation
        assert generation is not None

        report = harness.diagnostics.generation_pressure()

        assert report.current_generation_id == generation.generation_id
        assert report.live_generations == 1
        assert report.draining_generations == 0
        assert report.total_leases == 0
        assert report.oldest_lease_age_seconds is None
        assert report.history_retained == 0
        assert report.history_evicted == 0
        entry = report.generations[0]
        assert entry.is_current is True
        assert entry.state == "active"
        assert entry.leases == 0
        assert entry.age_seconds >= 0
        assert [plugin["entry_id"] for plugin in entry.retained_plugins] == ["db"]
        assert report.metrics()["chassis.generations.live"] == 1.0
    finally:
        await harness.stop()


async def test_a_leased_generation_reports_as_draining() -> None:
    harness = Harness()
    harness.install(guard_plugin("provider-a"), entry_id="db")
    await harness.start()
    try:
        generation_one = harness.current_generation
        assert generation_one is not None

        holder = Holder(harness)
        await holder.started()

        harness.install(guard_plugin("provider-b"), entry_id="db", replace=True)
        await harness.reconcile()
        generation_two = harness.current_generation
        assert generation_two is not None

        report = harness.diagnostics.generation_pressure()

        assert report.current_generation_id == generation_two.generation_id
        assert report.live_generations == 2
        assert report.draining_generations == 1
        assert report.total_leases == 1
        by_id = {entry.generation_id: entry for entry in report.generations}
        draining = by_id[generation_one.generation_id]
        assert draining.state == "draining"
        assert draining.is_current is False
        assert draining.leases == 1
        assert draining.oldest_lease_age_seconds is not None
        assert report.oldest_lease_age_seconds == draining.oldest_lease_age_seconds

        # The draining generation names the plugin instance it retains.
        retired_instance = generation_one.instances[0].instance_id
        assert [plugin["instance_id"] for plugin in draining.retained_plugins] == [retired_instance]

        await holder.finish()

        after = harness.diagnostics.generation_pressure()
        assert after.live_generations == 1
        assert after.oldest_lease_age_seconds is None
        assert retired_instance not in after.instance_generations
    finally:
        await harness.stop()


async def test_multiple_concurrent_leases_are_counted_per_generation() -> None:
    harness = Harness()
    harness.install(guard_plugin("provider-a"), entry_id="db")
    await harness.start()
    try:
        generation = harness.current_generation
        assert generation is not None

        entered = [asyncio.Event() for _ in range(3)]
        release = asyncio.Event()

        async def run(index: int) -> None:
            async with harness.acquire():
                entered[index].set()
                await release.wait()

        tasks = [asyncio.create_task(run(index)) for index in range(3)]
        for event in entered:
            await event.wait()

        report = harness.diagnostics.generation_pressure()

        assert report.total_leases == 3
        assert report.generations[0].leases == 3

        release.set()
        await asyncio.gather(*tasks)

        assert harness.diagnostics.generation_pressure().total_leases == 0
    finally:
        await harness.stop()


async def test_the_final_lease_release_allows_disposal() -> None:
    harness = Harness()
    harness.install(guard_plugin("provider-a"), entry_id="db")
    await harness.start()
    try:
        generation = harness.current_generation
        assert generation is not None
        instance = mounted(harness, "db")
        provider = generation.snapshot.require(DATABASE)

        holder = Holder(harness)
        await holder.started()

        harness.uninstall("db")
        await harness.reconcile()

        # Still retained: the lease keeps the generation, and the generation keeps
        # the plugin reachable, so it is not disposed.
        assert harness.diagnostics.instance_generations(instance.instance_id) == (
            generation.generation_id,
        )
        assert instance.state is PluginState.ACTIVE
        assert provider.disposed is False

        await holder.finish()

        assert provider.disposed is True
        assert instance.state is PluginState.DISPOSED
        assert harness.diagnostics.instance_generations(instance.instance_id) == ()
    finally:
        await harness.stop()


async def test_an_instance_reached_by_two_generations_names_both() -> None:
    harness = Harness()
    harness.install(guard_plugin("provider-a"), entry_id="db")
    await harness.start()
    try:
        generation_one = harness.current_generation
        assert generation_one is not None
        shared = mounted(harness, "db")

        holder = Holder(harness)
        await holder.started()

        @plugin(
            name="memory",
            version="1.0.0",
            provides={"memory": "1.0.0"},
            requires={"database": ">=1,<2"},
        )
        async def memory(ctx: PluginContext) -> None:
            ctx.capabilities.provide(MEMORY, Guard("memory"))

        harness.install(memory, entry_id="memory")
        await harness.reconcile()
        generation_two = harness.current_generation
        assert generation_two is not None

        report = harness.diagnostics.generation_pressure()

        assert report.instance_generations[shared.instance_id] == (
            generation_two.generation_id,
            generation_one.generation_id,
        )
        assert shared.generation_refs == 2

        await holder.finish()

        # Retiring generation one must not dispose the shared instance.
        assert shared.state is PluginState.ACTIVE
        assert harness.diagnostics.instance_generations(shared.instance_id) == (
            generation_two.generation_id,
        )
    finally:
        await harness.stop()


async def test_liveness_outlives_history_eviction() -> None:
    """The diagnostics buffer bounds *retired* history, never a live generation."""

    def extra_plugin(index: int):  # type: ignore[no-untyped-def]
        capability = CapabilityKey(f"extra-{index}", "1")

        @plugin(name=f"extra-{index}", version="1.0.0", provides={capability.name: "1.0.0"})
        async def extra(ctx: PluginContext) -> None:
            ctx.capabilities.provide(capability, Guard(f"extra-{index}"))

        return extra

    harness = Harness(generation_history_limit=1)
    harness.install(guard_plugin("provider-a"), entry_id="db")
    await harness.start()
    try:
        generation_one = harness.current_generation
        assert generation_one is not None

        holder = Holder(harness)
        await holder.started()

        harness.uninstall("db")
        for index in range(4):
            harness.install(extra_plugin(index), entry_id=f"extra-{index}")
            await harness.reconcile()

        report = harness.diagnostics.generation_pressure()

        assert report.history_limit == 1
        assert report.history_retained <= 1
        assert report.history_evicted >= 1
        assert generation_one.generation_id in {entry.generation_id for entry in report.generations}
        assert report.draining_generations == 1

        await holder.finish()

        assert generation_one.generation_id not in {
            entry.generation_id for entry in harness.diagnostics.generation_pressure().generations
        }
    finally:
        await harness.stop()


async def test_report_renders_text_and_structured_forms() -> None:
    harness = Harness()
    harness.install(guard_plugin("provider-a"), entry_id="db")
    await harness.start()
    try:
        report = harness.diagnostics.generation_pressure()
        payload = report.to_dict()
        text = report.to_text()

        assert payload["live_generations"] == 1
        assert payload["generations"][0]["retained_plugins"][0]["entry_id"] == "db"
        assert payload["history"] == {"limit": 32, "retained": 0, "evicted": 0}
        assert "current_generation:" in text
        assert "retained_plugins:" in text
        assert "db" in text
    finally:
        await harness.stop()
