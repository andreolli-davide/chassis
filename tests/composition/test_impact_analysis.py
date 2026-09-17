"""Incremental impact analysis: which nodes a change actually reaches.

Impact follows dependency bindings, not scope membership, so an unrelated sibling
subtree is reused. The tests read the analysis from the reconcile result and from
diagnostics, which share one implementation.
"""

from __future__ import annotations

from tests.composition.support import consumer, generation_of, mounted, tracked_provider

from chassis import Harness


def decisions(harness: Harness, impact_old: str, impact_new: str) -> dict[str, str]:
    analysis = harness.diagnostics.analyze_impact(impact_old, impact_new)
    return {node.entry_id: node.decision for node in analysis.nodes}


async def test_an_isolated_leaf_change_rebuilds_only_that_leaf() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db", config={"pool": 1})
    harness.install(tracked_provider("telemetry", "telemetry"), entry_id="telemetry")
    try:
        await harness.start()
        first = generation_of(harness)

        harness.install(
            tracked_provider("db", "database"), entry_id="db", config={"pool": 2}, replace=True
        )
        result = await harness.reconcile()
        second = generation_of(harness)

        assert decisions(harness, first.generation_id, second.generation_id) == {
            "db": "rebuilt",
            "telemetry": "reused",
        }
        assert result.impact is not None
        assert {node.entry_id for node in result.impact.reused} == {"telemetry"}
    finally:
        await harness.stop()


async def test_a_changed_provider_rebuilds_its_transitive_consumers() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db", config={"pool": 1})
    harness.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    try:
        await harness.start()
        first = generation_of(harness)
        agent_before = mounted(harness, "agent").instance_id

        harness.install(
            tracked_provider("db", "database"), entry_id="db", config={"pool": 2}, replace=True
        )
        result = await harness.reconcile()
        second = generation_of(harness)

        assert decisions(harness, first.generation_id, second.generation_id) == {
            "db": "rebuilt",
            "agent": "rewired",
        }
        assert mounted(harness, "agent").instance_id != agent_before
        assert result.impact is not None
        node = result.impact.get("agent")
        assert node is not None
        assert node.reasons == ("dependency_changed", "provider_selection_changed")
        assert node.dependency_changes == ("db",)
    finally:
        await harness.stop()


async def test_a_change_in_one_scope_does_not_rebuild_an_unrelated_sibling() -> None:
    harness = Harness()
    harness.composition.child("acme")
    harness.composition.child("globex")
    harness.install(
        tracked_provider("db", "database"), entry_id="db", scope="/acme", config={"pool": 1}
    )
    harness.install(tracked_provider("db", "database"), entry_id="globex-db", scope="/globex")
    try:
        await harness.start()
        first = generation_of(harness)
        globex_instance = mounted(harness, "globex-db").instance_id

        harness.install(
            tracked_provider("db", "database"),
            entry_id="db",
            scope="/acme",
            config={"pool": 2},
            replace=True,
        )
        await harness.reconcile()
        second = generation_of(harness)

        assert decisions(harness, first.generation_id, second.generation_id) == {
            "db": "rebuilt",
            "globex-db": "reused",
        }
        assert mounted(harness, "globex-db").instance_id == globex_instance
    finally:
        await harness.stop()


async def test_a_root_dependency_change_reaches_a_child_scope_consumer() -> None:
    harness = Harness()
    harness.composition.child("research")
    harness.install(tracked_provider("db", "database"), entry_id="db")
    harness.install(
        consumer("agent", requires={"database": ">=1,<2"}),
        entry_id="agent",
        scope="/research",
    )
    try:
        await harness.start()
        first = generation_of(harness)

        harness.install(tracked_provider("db2", "database"), entry_id="db", replace=True)
        await harness.reconcile()
        second = generation_of(harness)

        assert decisions(harness, first.generation_id, second.generation_id) == {
            "db": "rebuilt",
            "agent": "rewired",
        }
    finally:
        await harness.stop()


async def test_a_scope_visibility_change_is_reported_when_it_affects_dependencies() -> None:
    harness = Harness()
    research = harness.composition.child("research", capabilities=["database", "model"])
    harness.install(tracked_provider("db", "database"), entry_id="db", scope="/research")
    harness.install(tracked_provider("model", "model"), entry_id="model")
    harness.install(
        consumer(
            "agent",
            requires={"database": ">=1,<2"},
            optional={"model": ">=1,<2"},
        ),
        entry_id="agent",
        scope="/research",
    )
    try:
        await harness.start()
        first = generation_of(harness)

        research.restrict("database")
        result = await harness.reconcile()
        second = generation_of(harness)

        assert decisions(harness, first.generation_id, second.generation_id) == {
            "db": "reused",
            "model": "reused",
            "agent": "rewired",
        }
        node = result.impact.get("agent") if result.impact is not None else None
        assert node is not None
        assert "dependency_changed" in node.reasons
        assert "scope_visibility_changed" in node.reasons
    finally:
        await harness.stop()
