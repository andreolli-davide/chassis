"""Explain and diff diagnostics over scoped composition."""

from __future__ import annotations

import pytest
from tests.composition.support import (
    consumer,
    generation_of,
    mounted,
    scope_of,
    tracked_provider,
)

from chassis import DATABASE, MODEL, Harness
from chassis.core.errors import HarnessStateError


async def test_explain_selected_provider_names_origin_and_reason() -> None:
    harness = Harness()
    harness.install(tracked_provider("postgres", "database"), entry_id="postgres")
    research = harness.composition.child("research")
    research.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    try:
        await harness.start()
        explanation = harness.diagnostics.explain_requirement("agent", "database")

        assert explanation is not None
        assert explanation.status == "resolved"
        assert explanation.scope == "/research"
        assert explanation.consumer_kind == "plugin"
        assert explanation.generation_id == generation_of(harness).generation_id
        assert explanation.selected is not None
        assert explanation.selected["provider_entry_id"] == "postgres"
        assert explanation.selected["origin"] == "inherited"
        assert explanation.selected["scope"] == "/"
        assert explanation.reason == "only_eligible"
        assert explanation.to_dict()["selected"]["provider_name"] == "postgres"
        assert "postgres" in explanation.to_text()
    finally:
        await harness.stop()


async def test_explain_rejected_provider_reports_the_reason() -> None:
    harness = Harness()
    harness.install(tracked_provider("old-db", "database", version="1.2.0"), entry_id="old-db")
    harness.install(tracked_provider("new-db", "database", version="1.9.0"), entry_id="new-db")
    harness.install(consumer("agent", requires={"database": ">=1.5,<2"}), entry_id="agent")
    try:
        await harness.start()
        explanation = harness.diagnostics.explain_requirement("agent", "database")

        assert explanation is not None
        rejected = {item.provider_entry_id: item for item in explanation.candidates}
        assert rejected["old-db"].rejection == "version_mismatch"
        assert rejected["old-db"].eligible is False
        assert rejected["new-db"].selected is True
        assert "version_mismatch" in explanation.to_text()
    finally:
        await harness.stop()


async def test_explain_ambiguity_and_the_preference_that_resolves_it() -> None:
    harness = Harness()
    harness.install(tracked_provider("a-db", "database"), entry_id="a-db")
    harness.install(tracked_provider("b-db", "database"), entry_id="b-db")
    harness.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    try:
        await harness.start()
        ambiguous = harness.diagnostics.explain_requirement("agent", "database")

        assert ambiguous is not None
        assert ambiguous.status == "ambiguous"
        assert {item.rejection for item in ambiguous.candidates} == {"ambiguous"}

        harness.prefer_provider("database", "b-db", consumer="agent")
        resolved = harness.diagnostics.explain_requirement("agent", "database")

        assert resolved is not None
        assert resolved.status == "resolved"
        assert resolved.reason == "explicit_preference"
        assert resolved.selected is not None
        assert resolved.selected["provider_entry_id"] == "b-db"
    finally:
        await harness.stop()


async def test_explain_unresolved_requirement() -> None:
    harness = Harness()
    harness.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    try:
        await harness.start()
        explanation = harness.diagnostics.explain_requirement("agent", "database")

        assert explanation is not None
        assert explanation.status == "no_provider"
        assert explanation.selected is None
        assert explanation.candidates == ()
        assert "no provider" in explanation.reason
        assert "unresolved: no_provider" in explanation.to_text()
    finally:
        await harness.stop()


async def test_explain_requirement_for_a_scope_local_requirement() -> None:
    harness = Harness()
    harness.install(tracked_provider("model", "model"), entry_id="model")
    research = harness.composition.child("research")
    research.require(MODEL, ">=1,<2")
    try:
        await harness.start()
        explanation = harness.diagnostics.explain_requirement("/research", "model")

        assert explanation is not None
        assert explanation.consumer_kind == "scope"
        assert explanation.status == "resolved"
        assert explanation.selected is not None
        assert explanation.selected["origin"] == "inherited"
    finally:
        await harness.stop()


async def test_explain_requirement_returns_none_for_an_unknown_requirement() -> None:
    harness = Harness()
    try:
        await harness.start()
        assert harness.diagnostics.explain_requirement("nope", "database") is None
    finally:
        await harness.stop()


async def test_explain_scope_reports_visibility_and_ownership() -> None:
    harness = Harness()
    harness.redactor.add("sk-secret")
    harness.install(tracked_provider("model", "model"), entry_id="model")
    harness.install(tracked_provider("postgres", "database"), entry_id="postgres")
    research = harness.composition.child("research", capabilities=[MODEL, DATABASE])
    research.set_metadata({"team": "research", "credential": "sk-secret"})
    research.install(tracked_provider("local-db", "database"), entry_id="local-db")
    research.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    harness.prefer_provider("database", "local-db", scope="/research")
    try:
        await harness.start()
        explanation = harness.diagnostics.explain_scope("/research")

        assert explanation.parent == "/"
        assert explanation.children == ()
        assert explanation.capabilities == ("database", "model")
        assert explanation.entries == ("agent", "local-db")
        # Diagnostics identity mounted entries by their desired entry id.
        assert explanation.instances == ("agent", "local-db")
        # Local providers are the scope's own; inherited ones come from root.
        assert set(explanation.local_providers) == {"database"}
        # Diagnostics identity providers by their desired entry id.
        assert explanation.inherited_providers == {
            "database": ("postgres",),
            "model": ("model",),
        }
        assert explanation.owned_registrations
        assert explanation.unresolved == ()
        # Metadata is redacted, never emitted verbatim.
        assert explanation.metadata["team"] == "research"
        assert "sk-secret" not in str(explanation.to_dict())
        assert "scope: /research" in explanation.to_text()
    finally:
        await harness.stop()


async def test_explain_scope_reports_unresolved_requirements() -> None:
    harness = Harness()
    research = harness.composition.child("research")
    research.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    try:
        await harness.start()
        explanation = harness.diagnostics.explain_scope("/research")

        assert explanation.unresolved == ("agent database <2,>=1",)
        assert [item.status for item in explanation.requirements] == ["no_provider"]
    finally:
        await harness.stop()


async def test_explain_scope_rejects_an_unknown_scope() -> None:
    harness = Harness()
    try:
        await harness.start()
        with pytest.raises(HarnessStateError):
            harness.diagnostics.explain_scope("/missing")
    finally:
        await harness.stop()


async def test_scopes_diagnostics_follow_the_requested_generation() -> None:
    harness = Harness()
    harness.install(tracked_provider("postgres", "database"), entry_id="postgres")
    try:
        await harness.start()
        first = generation_of(harness)

        harness.composition.child("research")
        await harness.reconcile()
        second = generation_of(harness)

        assert [
            scope["path"] for scope in harness.diagnostics.scopes(generation_id=first.generation_id)
        ] == ["/"]
        assert [
            scope["path"]
            for scope in harness.diagnostics.scopes(generation_id=second.generation_id)
        ] == ["/", "/research"]
        assert harness.diagnostics.status()["composition"]["scopes"] == 2
        assert "scopes" in harness.diagnostics.dependencies()

        with pytest.raises(HarnessStateError):
            harness.diagnostics.scopes(generation_id="gen_9999")
    finally:
        await harness.stop()


async def test_diff_generations_reports_added_scope_and_rewired_requirement() -> None:
    harness = Harness()
    harness.install(tracked_provider("postgres", "database"), entry_id="postgres")
    harness.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    try:
        await harness.start()
        first = generation_of(harness)

        research = harness.composition.child("research")
        research.install(
            consumer("researcher", requires={"database": ">=1,<2"}), entry_id="researcher"
        )
        harness.prefer_provider("database", "postgres", consumer="researcher")
        await harness.reconcile()
        second = generation_of(harness)

        diff = harness.diagnostics.diff_generations(first.generation_id, second.generation_id)

        scope_changes = {(item.kind, item.subject) for item in diff.scopes}
        assert ("added", "/research") in scope_changes
        # The root gained a child, so its own structure changed too.
        assert ("changed", "/") in scope_changes
        assert [(item.kind, item.subject) for item in diff.providers] == [("added", "researcher")]
        added = [item for item in diff.requirements if item.kind == "added"]
        assert [item.subject for item in added] == ["/research::researcher::database"]
        assert "SCOPES" in diff.to_text()
        assert diff.to_dict()["new_generation_id"] == second.generation_id
    finally:
        await harness.stop()


async def test_diff_generations_reports_removed_scope_and_provider() -> None:
    harness = Harness()
    harness.composition.child("research").install(
        tracked_provider("search", "search"), entry_id="search"
    )
    try:
        await harness.start()
        first = generation_of(harness)

        harness.composition.remove("/research")
        await harness.reconcile()
        second = generation_of(harness)

        diff = harness.diagnostics.diff_generations(first.generation_id, second.generation_id)

        scope_changes = {(item.kind, item.subject) for item in diff.scopes}
        assert ("removed", "/research") in scope_changes
        assert ("changed", "/") in scope_changes
        assert [(item.kind, item.subject) for item in diff.providers] == [("removed", "search")]
    finally:
        await harness.stop()


async def test_diff_generations_reports_rewired_requirements_conservatively() -> None:
    harness = Harness()
    harness.install(tracked_provider("db-a", "database"), entry_id="db-a")
    harness.install(tracked_provider("db-b", "database"), entry_id="db-b")
    harness.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    harness.prefer_provider("database", "db-a", consumer="agent")
    try:
        await harness.start()
        first = generation_of(harness)

        harness.prefer_provider("database", "db-b", consumer="agent")
        await harness.reconcile()
        second = generation_of(harness)

        diff = harness.diagnostics.diff_generations(first.generation_id, second.generation_id)

        rewired = diff.requirements
        assert len(rewired) == 1
        assert (rewired[0].kind, rewired[0].old, rewired[0].new) == ("rewired", "db-a", "db-b")
        assert "explicit provider preference" in rewired[0].reason
        # Reuse is claimed only for identical instances, and only when asked for.
        assert diff.providers == ()
        with_unchanged = harness.diagnostics.diff_generations(
            first.generation_id, second.generation_id, include_unchanged=True
        )
        assert {item.subject for item in with_unchanged.providers} == {"db-a", "db-b", "agent"}
    finally:
        await harness.stop()


async def test_diff_generations_rejects_an_unknown_generation() -> None:
    harness = Harness()
    try:
        await harness.start()
        with pytest.raises(HarnessStateError):
            harness.diagnostics.diff_generations("gen_0001", "gen_9999")
    finally:
        await harness.stop()


async def test_snapshot_represents_scoped_composition_without_config_values() -> None:
    secret = "sk-live-abcdef123456"
    harness = Harness()
    harness.redactor.add(secret)
    harness.install(tracked_provider("model", "model"), entry_id="model")
    research = harness.composition.child(
        "research", capabilities=[MODEL], metadata={"team": secret}
    )
    research.install(
        consumer("agent", requires={"model": ">=1,<2"}),
        entry_id="agent",
        config={"api_key": secret},
    )
    try:
        await harness.start()
        snapshot = harness.snapshot_for(generation_of(harness))
        payload = snapshot.to_dict()

        scopes = {scope["path"]: scope for scope in payload["scopes"]["scopes"]}
        assert set(scopes) == {"/", "/research"}
        assert scopes["/research"]["parent"] == "/"
        assert scopes["/research"]["capabilities"] == ["model"]
        assert scopes["/research"]["entries"] == ["agent"]
        assert scopes["/research"]["providers"] == {}
        # The provider itself lives at the root; the scope records the selection.
        assert scopes["/"]["providers"] == {"model": [mounted(harness, "model").instance_id]}
        selections = scopes["/research"]["selections"]
        assert selections[0]["consumer"] == "agent"
        assert selections[0]["provider"] == "model"
        assert selections[0]["provider_scope"] == "/"

        rendered = str(payload)
        assert secret not in rendered
        assert "api_key" not in rendered
        assert snapshot.digest() == snapshot.digest()
    finally:
        await harness.stop()


async def test_snapshot_digest_tracks_scope_topology_and_resolution() -> None:
    harness = Harness()
    harness.install(tracked_provider("db-a", "database"), entry_id="db-a")
    harness.install(tracked_provider("db-b", "database"), entry_id="db-b")
    research = harness.composition.child("research")
    research.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    try:
        await harness.start()
        ambiguous = harness.snapshot_for(generation_of(harness)).digest()

        # Resolving the requirement changes the composition a run observes.
        harness.prefer_provider("database", "db-b", consumer="agent")
        await harness.reconcile()
        resolved = harness.snapshot_for(generation_of(harness)).digest()
        assert resolved != ambiguous

        # Re-publishing the same composition changes nothing.
        await harness.reconcile()
        assert harness.snapshot_for(generation_of(harness)).digest() == resolved

        # Topology alone is observable too, even without a provider change.
        research.restrict(DATABASE)
        await harness.reconcile()
        restricted = harness.snapshot_for(generation_of(harness)).digest()
        assert restricted != resolved
        assert restricted != ambiguous
    finally:
        await harness.stop()


async def test_scope_of_a_published_generation_is_cheap_to_inspect() -> None:
    harness = Harness()
    harness.composition.child("tenant").child("research")
    try:
        await harness.start()
        scope = scope_of(harness, "/tenant/research")

        assert scope.parent == "/tenant"
        assert scope.children == ()
        assert scope.capabilities is None
        assert harness.diagnostics.explain_scope("/tenant").children == ("/tenant/research",)
    finally:
        await harness.stop()
