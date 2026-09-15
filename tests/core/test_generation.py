from __future__ import annotations

import asyncio

import pytest

from chassis.capabilities.snapshot import CapabilitySnapshot
from chassis.core.errors import GenerationConflictError, HarnessStateError
from chassis.core.generation import GenerationState
from chassis.core.generations import GenerationManager
from chassis.core.scope import Scope
from chassis.plugins.lifecycle import PluginInstance, PluginState
from chassis.plugins.manifest import PluginManifest


def instance(name: str) -> PluginInstance:
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


@pytest.fixture
def manager() -> GenerationManager:
    return GenerationManager()


def test_acquisition_requires_a_published_generation(manager: GenerationManager) -> None:
    assert manager.current is None
    with pytest.raises(HarnessStateError):
        manager.acquire()

    candidate = manager.build(snapshot_factory=empty_snapshot, instances=[])
    assert candidate.state is GenerationState.BUILDING

    # A candidate is invisible, so acquisition is still refused.
    with pytest.raises(HarnessStateError):
        manager.acquire()


def test_publish_marks_the_previous_generation_draining(manager: GenerationManager) -> None:
    first = manager.build(snapshot_factory=empty_snapshot, instances=[instance("a")])
    second = manager.build(snapshot_factory=empty_snapshot, instances=[instance("a")])

    assert manager.publish(first) is None
    assert manager.publish(second) is first

    assert first.state is GenerationState.DRAINING
    assert second.state is GenerationState.ACTIVE
    assert manager.current is second


def test_release_reports_only_the_last_lease_of_a_draining_generation(
    manager: GenerationManager,
) -> None:
    first = manager.build(snapshot_factory=empty_snapshot, instances=[])
    manager.publish(first)
    manager.acquire()
    manager.acquire()
    manager.publish(manager.build(snapshot_factory=empty_snapshot, instances=[]))

    assert first.state is GenerationState.DRAINING
    assert first.lease_count == 2

    assert manager.release(first) is False
    assert manager.release(first) is True


def test_release_of_an_active_generation_is_not_a_reclaim_signal(
    manager: GenerationManager,
) -> None:
    active = manager.build(snapshot_factory=empty_snapshot, instances=[])
    manager.publish(active)

    manager.acquire()
    assert manager.release(active) is False


def test_retiring_an_active_generation_is_rejected(manager: GenerationManager) -> None:
    active = manager.build(snapshot_factory=empty_snapshot, instances=[])
    manager.publish(active)

    with pytest.raises(GenerationConflictError):
        manager.retire(active)


def test_publishing_a_non_building_generation_is_rejected(manager: GenerationManager) -> None:
    active = manager.build(snapshot_factory=empty_snapshot, instances=[])
    manager.publish(active)

    with pytest.raises(GenerationConflictError):
        manager.publish(active)


def test_discarding_a_candidate_leaves_it_unpublished(manager: GenerationManager) -> None:
    candidate = manager.build(snapshot_factory=empty_snapshot, instances=[])

    assert manager.discard(candidate) is True
    assert candidate.state is GenerationState.RETIRED
    assert manager.current is None
    assert manager.history == ()


def test_reachability_spans_draining_generations(manager: GenerationManager) -> None:
    shared = instance("shared")
    leaving = instance("leaving")

    retained = manager.build(snapshot_factory=empty_snapshot, instances=[shared, leaving])
    manager.publish(retained)
    manager.publish(manager.build(snapshot_factory=empty_snapshot, instances=[shared]))

    assert manager.reachable_instance_ids() == {"plugin_shared", "plugin_leaving"}
    assert manager.draining() == (retained,)

    manager.retire(retained)

    assert manager.reachable_instance_ids() == {"plugin_shared"}
    assert manager.draining() == ()


def test_reference_counting_follows_live_generations(manager: GenerationManager) -> None:
    shared = instance("shared")
    only_first = instance("first")

    retained = manager.build(snapshot_factory=empty_snapshot, instances=[shared, only_first])
    manager.publish(retained)
    manager.publish(manager.build(snapshot_factory=empty_snapshot, instances=[shared]))

    instances = [shared, only_first]
    manager.refresh_references(instances)
    assert shared.generation_refs == 2
    assert only_first.generation_refs == 1

    manager.retire(retained)
    manager.refresh_references(instances)

    assert shared.generation_refs == 1
    assert only_first.generation_refs == 0


def test_begin_shutdown_stops_new_acquisitions(manager: GenerationManager) -> None:
    active = manager.build(snapshot_factory=empty_snapshot, instances=[])
    manager.publish(active)

    draining = manager.begin_shutdown()

    assert draining == (active,)
    assert manager.current is None
    with pytest.raises(HarnessStateError):
        manager.acquire()


async def test_drain_waits_for_released_leases(manager: GenerationManager) -> None:
    active = manager.build(snapshot_factory=empty_snapshot, instances=[])
    manager.publish(active)
    manager.acquire()
    draining = manager.begin_shutdown()

    waiter = asyncio.ensure_future(manager.drain(draining))
    await asyncio.sleep(0)
    assert not waiter.done()

    manager.release(active)

    idle, busy = await waiter
    assert idle == (active,)
    assert busy == ()


async def test_drain_reports_generations_that_outlive_the_timeout(
    manager: GenerationManager,
) -> None:
    active = manager.build(snapshot_factory=empty_snapshot, instances=[])
    manager.publish(active)
    manager.acquire()
    draining = manager.begin_shutdown()

    idle, busy = await manager.drain(draining, timeout_seconds=0.01)

    assert idle == ()
    assert busy == (active,)
    manager.release(active)


def test_history_is_bounded_and_newest_first(manager: GenerationManager) -> None:
    small = GenerationManager(history_limit=2)
    generations = []
    for _ in range(4):
        generation = small.build(snapshot_factory=empty_snapshot, instances=[])
        small.publish(generation)
        generations.append(generation)

    # Only the most recent generations are retained for diagnostics.
    assert [item.generation_id for item in small.all_generations()] == [
        generations[3].generation_id,
        generations[2].generation_id,
        generations[1].generation_id,
    ]
    assert small.current is generations[3]

    for generation in small.draining():
        small.retire(generation)

    assert [item.generation_id for item in small.history] == [
        generations[2].generation_id,
        generations[1].generation_id,
    ]


def test_generation_and_snapshot_are_immutable() -> None:
    generation = GenerationManager().build(snapshot_factory=empty_snapshot, instances=[])

    with pytest.raises(AttributeError):
        generation.sequence = 9  # type: ignore[misc]
    with pytest.raises(AttributeError):
        generation.snapshot = None  # type: ignore[misc]


def test_generation_metadata_is_read_only() -> None:
    generation = GenerationManager().build(
        snapshot_factory=empty_snapshot, instances=[], metadata={"plugins": 1}
    )

    assert generation.metadata["plugins"] == 1
    with pytest.raises(TypeError):
        generation.metadata["plugins"] = 2  # type: ignore[index]


def test_generation_diagnostics_include_leases_and_plugins() -> None:
    manager = GenerationManager()
    generation = manager.build(snapshot_factory=empty_snapshot, instances=[instance("a")])
    manager.publish(generation)

    payload = generation.to_dict()

    assert payload["generation_id"] == "gen_0001"
    assert payload["state"] == "active"
    assert payload["leases"] == 0
    assert payload["plugins"] == {"a": "1.0.0"}
    assert payload["instances"] == ["plugin_a"]


def test_generation_ids_are_sequential() -> None:
    manager = GenerationManager()
    ids = [
        manager.build(snapshot_factory=empty_snapshot, instances=[]).generation_id for _ in range(3)
    ]

    assert ids == ["gen_0001", "gen_0002", "gen_0003"]
