"""Why a node was reused, rebuilt, rewired, added, or removed.

The explanation is built from the semantic identities published with each
generation, and it keeps physical reuse distinct from semantic sameness.
"""

from __future__ import annotations

from tests.composition.support import consumer, generation_of, mounted, tracked_provider

from chassis import Harness


async def test_explain_reports_physical_reuse_with_the_shared_instance() -> None:
    harness = Harness()
    harness.install(tracked_provider("telemetry", "telemetry"), entry_id="telemetry")
    try:
        await harness.start()
        first = generation_of(harness)
        instance_id = mounted(harness, "telemetry").instance_id

        harness.install(tracked_provider("db", "database"), entry_id="db")
        await harness.reconcile()
        second = generation_of(harness)

        explained = harness.diagnostics.explain_reuse(
            first.generation_id, second.generation_id, "telemetry"
        )
        assert explained is not None
        assert explained.decision == "reused"
        assert explained.shared_instance_id == instance_id
        assert explained.physically_reused is True
        assert explained.semantically_unchanged is True
        assert explained.reasons == ()
    finally:
        await harness.stop()


async def test_explain_reports_a_rebuild_and_its_reason() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db", config={"pool": 1})
    try:
        await harness.start()
        first = generation_of(harness)

        harness.install(
            tracked_provider("db", "database"), entry_id="db", config={"pool": 2}, replace=True
        )
        await harness.reconcile()
        second = generation_of(harness)

        explained = harness.diagnostics.explain_reuse(
            first.generation_id, second.generation_id, "db"
        )
        assert explained is not None
        assert explained.decision == "rebuilt"
        assert explained.reasons == ("config_changed",)
        assert explained.changed_inputs == ("config",)
        assert explained.shared_instance_id is None
        assert explained.to_dict()["new_semantic_id"] != explained.to_dict()["old_semantic_id"]
    finally:
        await harness.stop()


async def test_explain_reports_a_rewire_and_the_dependency_that_changed() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db")
    harness.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    try:
        await harness.start()
        first = generation_of(harness)

        harness.install(tracked_provider("db2", "database"), entry_id="db", replace=True)
        await harness.reconcile()
        second = generation_of(harness)

        explained = harness.diagnostics.explain_reuse(
            first.generation_id, second.generation_id, "agent"
        )
        assert explained is not None
        assert explained.decision == "rewired"
        assert explained.dependency_changes == ("db",)
        assert explained.changed_inputs == ("dependencies",)
        assert explained.physically_reused is False
    finally:
        await harness.stop()


async def test_a_secret_only_change_forces_a_rebuild_but_leaks_nothing() -> None:
    secret = "sk-live-abcdef123456"
    harness = Harness()
    harness.redactor.add(secret)
    harness.install(tracked_provider("db", "database"), entry_id="db", config={"api_key": secret})
    try:
        await harness.start()
        first = generation_of(harness)

        harness.install(
            tracked_provider("db", "database"),
            entry_id="db",
            config={"api_key": "sk-live-999999999999"},
            replace=True,
        )
        await harness.reconcile()
        second = generation_of(harness)

        explained = harness.diagnostics.explain_reuse(
            first.generation_id, second.generation_id, "db"
        )
        assert explained is not None
        # The change is detected through the private fingerprint...
        assert explained.decision == "rebuilt"
        assert explained.reasons == ("config_changed",)
        # ...but the secret-derived value never reaches the explanation.
        assert explained.old_semantic_id == explained.new_semantic_id
        assert secret not in str(explained.to_dict())
        instance = mounted(harness, "db")
        assert instance.semantic_identity is not None
        assert secret not in str(instance.semantic_identity.to_dict())
    finally:
        await harness.stop()


async def test_explain_reports_added_and_removed_nodes() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db")
    try:
        await harness.start()
        first = generation_of(harness)

        harness.install(tracked_provider("telemetry", "telemetry"), entry_id="telemetry")
        await harness.reconcile()
        second = generation_of(harness)
        added = harness.diagnostics.explain_reuse(
            first.generation_id, second.generation_id, "telemetry"
        )
        assert added is not None and added.decision == "added"

        harness.uninstall("db")
        await harness.reconcile()
        third = generation_of(harness)
        removed = harness.diagnostics.explain_reuse(second.generation_id, third.generation_id, "db")
        assert removed is not None and removed.decision == "removed"
    finally:
        await harness.stop()


async def test_diff_distinguishes_semantic_sameness_from_physical_reuse() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db")
    harness.install(tracked_provider("telemetry", "telemetry"), entry_id="telemetry")
    try:
        await harness.start()
        first = generation_of(harness)

        # Replacing a node with a semantically identical one forces a new
        # revision, hence a new instance, without changing behaviour.
        harness.install(tracked_provider("db", "database"), entry_id="db", replace=True)
        await harness.reconcile()
        second = generation_of(harness)

        diff = harness.diagnostics.diff_generations(
            first.generation_id, second.generation_id, include_unchanged=True
        )
        by_entry = {node.entry_id: node for node in diff.nodes}
        assert by_entry["telemetry"].decision == "reused"
        assert by_entry["db"].decision == "unchanged"
        assert by_entry["db"].physically_reused is False
        assert by_entry["db"].semantically_unchanged is True
    finally:
        await harness.stop()


async def test_pressure_shows_a_resource_reachable_from_several_generations() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db")
    try:
        await harness.start()
        first = generation_of(harness)
        async with harness.acquire():
            harness.install(tracked_provider("mem", "memory"), entry_id="mem")
            await harness.reconcile()
            second = generation_of(harness)

            report = harness.diagnostics.generation_pressure()
            shared = next(item for item in report.resources if item.entry_id == "db")
            assert shared.shared is True
            assert set(shared.generations) == {first.generation_id, second.generation_id}
            assert "sharing" in shared.retained_by
            assert "lease" in shared.retained_by
            assert report.metrics()["chassis.resources.shared"] == 1.0
    finally:
        await harness.stop()
