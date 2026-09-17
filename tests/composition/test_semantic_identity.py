"""Semantic identity: what makes two composition nodes reuse-safe.

These tests pin the identity inputs that are intended to affect reuse and, just
as importantly, the ones that are not (a preference that selects the same
provider, volatile metadata).
"""

from __future__ import annotations

from tests.composition.support import consumer, mounted, tracked_provider

from chassis import Harness, plugin
from chassis.core.identity import SemanticIdentity


def identity_of(harness: Harness, entry_id: str) -> SemanticIdentity:
    instance = mounted(harness, entry_id)
    assert instance.semantic_identity is not None
    return instance.semantic_identity


async def test_identical_node_reports_the_same_semantic_identity() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db")
    try:
        await harness.start()
        first = identity_of(harness, "db")
        instance_id = mounted(harness, "db").instance_id

        result = await harness.reconcile()

        assert identity_of(harness, "db") == first
        assert mounted(harness, "db").instance_id == instance_id
        assert result.impact is not None
        node = result.impact.get("db")
        assert node is not None
        assert node.decision == "reused"
    finally:
        await harness.stop()


async def test_configuration_change_changes_the_config_input_only() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db", config={"pool": 1})
    try:
        await harness.start()
        first = identity_of(harness, "db")

        harness.install(
            tracked_provider("db", "database"), entry_id="db", config={"pool": 2}, replace=True
        )
        await harness.reconcile()
        second = identity_of(harness, "db")

        assert first.implementation_fingerprint == second.implementation_fingerprint
        assert first.contract_fingerprint == second.contract_fingerprint
        assert first.config_fingerprint != second.config_fingerprint
        assert second.changed_inputs(first) == ("config",)
    finally:
        await harness.stop()


async def test_implementation_change_changes_the_implementation_input() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db")
    try:
        await harness.start()
        first = identity_of(harness, "db")

        harness.install(
            tracked_provider("db", "database", version="1.1.0"), entry_id="db", replace=True
        )
        await harness.reconcile()
        second = identity_of(harness, "db")

        assert first.implementation_fingerprint != second.implementation_fingerprint
        assert "implementation" in second.changed_inputs(first)
    finally:
        await harness.stop()


async def test_scope_change_changes_the_scope_input() -> None:
    harness = Harness()
    harness.composition.child("research")
    harness.install(tracked_provider("db", "database"), entry_id="db")
    try:
        await harness.start()
        first = identity_of(harness, "db")

        harness.install(
            tracked_provider("db", "database"), entry_id="db", replace=True, scope="/research"
        )
        await harness.reconcile()
        second = identity_of(harness, "db")

        assert first.scope_path == "/"
        assert second.scope_path == "/research"
        assert "scope" in second.changed_inputs(first)
    finally:
        await harness.stop()


async def test_dependency_change_changes_the_dependency_input() -> None:
    harness = Harness()
    harness.install(tracked_provider("db-a", "database"), entry_id="db-a")
    harness.install(tracked_provider("db-b", "database"), entry_id="db-b")
    harness.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    harness.prefer_provider("database", "db-a", consumer="agent")
    try:
        await harness.start()
        first = identity_of(harness, "agent")
        assert first.bindings[0].provider_entry_id == "db-a"

        harness.prefer_provider("database", "db-b", consumer="agent")
        await harness.reconcile()
        second = identity_of(harness, "agent")

        assert second.bindings[0].provider_entry_id == "db-b"
        assert second.changed_inputs(first) == ("dependencies",)
    finally:
        await harness.stop()


async def test_a_preference_that_selects_the_same_provider_is_not_relevant() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db")
    harness.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    try:
        await harness.start()
        first = identity_of(harness, "agent")
        instance_id = mounted(harness, "agent").instance_id

        # The provider was already the only eligible one, so the preference does
        # not change behaviour and must not force a rebuild.
        harness.prefer_provider("database", "db", consumer="agent")
        result = await harness.reconcile()

        assert identity_of(harness, "agent") == first
        assert mounted(harness, "agent").instance_id == instance_id
        assert result.impact is not None
        node = result.impact.get("agent")
        assert node is not None
        assert node.decision == "reused"
    finally:
        await harness.stop()


async def test_semantically_equal_ignores_provider_instance_identity() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db")
    harness.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    try:
        await harness.start()
        before = identity_of(harness, "agent")

        # A replacement provider with identical behaviour is rebuilt for its own
        # reasons, but the consumer's semantic identity is unchanged.
        harness.install(tracked_provider("db", "database"), entry_id="db", replace=True)
        await harness.reconcile()
        after = identity_of(harness, "agent")

        assert after.semantically_equal(before)
        assert after.reuse_key() != before.reuse_key()
    finally:
        await harness.stop()


def build(implementation_revision: str | None = None):  # type: ignore[no-untyped-def]
    """A plugin whose manifest declares an author-declared implementation revision."""

    @plugin(
        name="generated",
        version="1.0.0",
        provides={"database": "1.0.0"},
        implementation_revision=implementation_revision,
    )
    async def generated(ctx) -> None:  # type: ignore[no-untyped-def]
        return None

    return generated


async def test_implementation_revision_changes_the_implementation_input() -> None:
    """Two builds that share name@version and qualname are still distinguishable."""

    harness = Harness()
    harness.install(build("a"), entry_id="db")
    try:
        await harness.start()
        first = identity_of(harness, "db")

        harness.install(build("b"), entry_id="db", replace=True)
        await harness.reconcile()
        second = identity_of(harness, "db")

        assert first.contract_fingerprint == second.contract_fingerprint
        assert first.implementation_fingerprint != second.implementation_fingerprint
        assert "implementation" in second.changed_inputs(first)
    finally:
        await harness.stop()


def test_implementation_revision_is_optional_and_distinguishing() -> None:
    """Two builds that share name@version and qualname are still distinguishable."""

    from chassis.core.identity import implementation_fingerprint

    assert build().manifest.implementation_revision is None
    assert build("a").manifest.implementation_revision == "a"
    assert implementation_fingerprint(build("a").manifest, "m.q") != implementation_fingerprint(
        build("b").manifest, "m.q"
    )
