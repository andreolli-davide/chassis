"""Scope hierarchy: identity, visibility, inheritance, narrowing, immutability.

These tests are the specification of record for scoped composition semantics.
"""

from __future__ import annotations

import dataclasses

import pytest
from tests.composition.support import (
    consumer,
    generation_of,
    mounted,
    scope_of,
    tracked_provider,
)

from chassis import DATABASE, MODEL, Harness
from chassis.composition import (
    ROOT_PATH,
    CompositionScope,
    CompositionTree,
    ResolvedScope,
    ScopeTree,
)
from chassis.core.errors import ConfigurationError

# --------------------------------------------------------------------- identity


def test_root_scope_is_the_default_and_is_a_real_scope() -> None:
    harness = Harness()

    assert harness.composition.root.path == ROOT_PATH
    assert harness.composition.paths() == (ROOT_PATH,)
    tree = ScopeTree.root_only()
    root = tree.get(ROOT_PATH)

    assert tree.paths() == (ROOT_PATH,)
    assert root is not None
    assert root.entries == ()


def test_scope_paths_are_derived_from_names_not_object_identity() -> None:
    harness = Harness()
    acme = harness.composition.child("tenant:acme")
    research = acme.child("research")

    assert acme.path == "/tenant:acme"
    assert research.path == "/tenant:acme/research"
    assert harness.composition.paths() == (ROOT_PATH, "/tenant:acme", "/tenant:acme/research")
    # A second harness with the same names produces the same paths.
    other = Harness()
    assert other.composition.child("tenant:acme").child("research").path == "/tenant:acme/research"


def test_scope_names_reject_the_path_separator() -> None:
    tree = CompositionTree()

    with pytest.raises(ConfigurationError):
        tree.child("nested/name")
    with pytest.raises(ConfigurationError):
        tree.child("")


def test_installing_into_an_undeclared_scope_is_refused() -> None:
    harness = Harness()

    with pytest.raises(ConfigurationError):
        harness.install(tracked_provider("db", "database"), entry_id="db", scope="/nope")


def test_removing_the_root_scope_is_refused() -> None:
    harness = Harness()

    with pytest.raises(ConfigurationError):
        harness.composition.remove(ROOT_PATH)


# ------------------------------------------------------------------- hierarchy


async def test_root_only_composition_resolves_as_before() -> None:
    harness = Harness()
    harness.install(tracked_provider("db", "database"), entry_id="db")
    harness.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    try:
        result = await harness.start()

        assert result.plan.activation_order == ("db", "agent")
        # An undeclared hierarchy still publishes a real root scope.
        assert generation_of(harness).scopes.paths() == (ROOT_PATH,)
        assert scope_of(harness, ROOT_PATH).entries == ("agent", "db")
    finally:
        await harness.stop()


async def test_child_scope_inherits_a_provider_from_the_root() -> None:
    harness = Harness()
    harness.install(tracked_provider("postgres", "database"), entry_id="postgres")
    research = harness.composition.child("research")
    research.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    try:
        await harness.start()
        resolved = generation_of(harness).scopes.get("/research")

        assert resolved is not None
        assert resolved.inherited == {"database": (mounted(harness, "postgres").instance_id,)}
        assert resolved.providers == {}
        explanation = harness.diagnostics.explain_requirement("agent", "database")
        assert explanation is not None
        assert explanation.selected is not None
        assert explanation.selected["origin"] == "inherited"
        assert explanation.selected["scope"] == ROOT_PATH
    finally:
        await harness.stop()


async def test_local_provider_resolves_within_its_own_scope() -> None:
    harness = Harness()
    finance = harness.composition.child("finance")
    finance.install(tracked_provider("erp", "erp"), entry_id="erp")
    finance.install(consumer("ledger", requires={"erp": ">=1,<2"}), entry_id="ledger")
    try:
        result = await harness.start()

        assert result.plan.activation_order == ("erp", "ledger")
        resolved = generation_of(harness).scopes.get("/finance")
        assert resolved is not None
        assert resolved.providers == {"erp": (mounted(harness, "erp").instance_id,)}
        explanation = harness.diagnostics.explain_requirement("ledger", "erp")
        assert explanation is not None
        assert explanation.selected is not None
        assert explanation.selected["origin"] == "local"
    finally:
        await harness.stop()


async def test_nested_scopes_resolve_and_inherit_through_the_path() -> None:
    harness = Harness()
    harness.install(tracked_provider("postgres", "database"), entry_id="postgres")
    research = harness.composition.child("tenant:acme").child("research")
    research.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    try:
        await harness.start()

        assert generation_of(harness).scopes.paths() == (
            ROOT_PATH,
            "/tenant:acme",
            "/tenant:acme/research",
        )
        assert scope_of(harness, "/tenant:acme/research").inherited
        assert harness.diagnostics.explain_scope("/tenant:acme/research").parent == "/tenant:acme"
    finally:
        await harness.stop()


# ------------------------------------------------------------------ invisibility


async def test_a_parent_never_sees_a_child_only_provider() -> None:
    harness = Harness()
    child = harness.composition.child("research")
    child.install(tracked_provider("search", "search"), entry_id="search")
    harness.install(consumer("root-agent", requires={"search": ">=1,<2"}), entry_id="root-agent")
    try:
        result = await harness.start()

        assert result.plan.pending == ("root-agent",)
        explanation = harness.diagnostics.explain_requirement("root-agent", "search")
        assert explanation is not None
        assert explanation.status == "not_visible"
        rejected = [item for item in explanation.candidates if item.rejection == "not_visible"]
        assert [item.provider_entry_id for item in rejected] == ["search"]
    finally:
        await harness.stop()


async def test_sibling_scopes_do_not_see_each_others_private_providers() -> None:
    harness = Harness()
    research = harness.composition.child("research")
    finance = harness.composition.child("finance")
    research.install(tracked_provider("research-db", "database"), entry_id="research-db")
    finance.install(tracked_provider("finance-db", "database"), entry_id="finance-db")
    research.install(consumer("r-agent", requires={"database": ">=1,<2"}), entry_id="r-agent")
    finance.install(consumer("f-agent", requires={"database": ">=1,<2"}), entry_id="f-agent")
    try:
        result = await harness.start()

        assert result.plan.activation_order == (
            "finance-db",
            "f-agent",
            "research-db",
            "r-agent",
        )
        research_scope = generation_of(harness).scopes.get("/research")
        finance_scope = generation_of(harness).scopes.get("/finance")
        assert research_scope is not None and finance_scope is not None
        research_db = mounted(harness, "research-db").instance_id
        finance_db = mounted(harness, "finance-db").instance_id
        assert research_scope.visible["database"] == (research_db,)
        assert finance_scope.visible["database"] == (finance_db,)
        assert scope_of(harness, ROOT_PATH).visible == {}
    finally:
        await harness.stop()


# ------------------------------------------------------------ capability narrowing


async def test_capability_narrowing_hides_an_inherited_capability() -> None:
    harness = Harness()
    harness.install(tracked_provider("model", "model"), entry_id="model")
    harness.install(tracked_provider("postgres", "database"), entry_id="postgres")
    harness.install(tracked_provider("scheduler", "scheduler"), entry_id="scheduler")
    research = harness.composition.child("research", capabilities=[MODEL, DATABASE])
    research.install(consumer("agent", requires={"scheduler": ">=1,<2"}), entry_id="agent")
    try:
        result = await harness.start()

        assert result.plan.pending == ("agent",)
        resolved = generation_of(harness).scopes.get("/research")
        assert resolved is not None
        assert resolved.capabilities == ("database", "model")
        assert "scheduler" not in resolved.visible
        explanation = harness.diagnostics.explain_requirement("agent", "scheduler")
        assert explanation is not None
        assert explanation.status == "not_visible"
        assert explanation.candidates[0].rejection == "capability_not_exposed"
    finally:
        await harness.stop()


async def test_narrowing_intersects_along_the_lineage() -> None:
    harness = Harness()
    harness.install(tracked_provider("model", "model"), entry_id="model")
    harness.install(tracked_provider("postgres", "database"), entry_id="postgres")
    tenant = harness.composition.child("tenant", capabilities=[MODEL])
    research = tenant.child("research", capabilities=[MODEL, DATABASE])
    research.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    try:
        result = await harness.start()

        # A descendant cannot widen: `research` declares `database`, but `tenant`
        # does not expose it, so the intersection still hides it.
        assert result.plan.pending == ("agent",)
        resolved = generation_of(harness).scopes.get("/tenant/research")
        assert resolved is not None
        assert resolved.capabilities == ("model",)
    finally:
        await harness.stop()


async def test_a_local_provider_outside_the_view_is_not_visible() -> None:
    harness = Harness()
    harness.install(tracked_provider("model", "model"), entry_id="model")
    child = harness.composition.child("research", capabilities=[MODEL])
    child.install(tracked_provider("local-db", "database"), entry_id="local-db")
    child.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    try:
        result = await harness.start()

        # The view is the composition a scope may observe, local providers
        # included: an excluded local provider is not silently reinstated.
        assert result.plan.pending == ("agent",)
        resolved = generation_of(harness).scopes.get("/research")
        assert resolved is not None
        assert "database" not in resolved.visible
    finally:
        await harness.stop()


# ------------------------------------------------------ local vs inherited provider


async def test_local_and_inherited_providers_are_ambiguous_without_a_preference() -> None:
    harness = Harness()
    harness.install(tracked_provider("shared-db", "database"), entry_id="shared-db")
    research = harness.composition.child("research")
    research.install(tracked_provider("local-db", "database"), entry_id="local-db")
    research.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    try:
        result = await harness.start()

        assert result.plan.pending == ("agent",)
        explanation = harness.diagnostics.explain_requirement("agent", "database")
        assert explanation is not None
        assert explanation.status == "ambiguous"
        assert {item.provider_entry_id for item in explanation.candidates} == {
            "shared-db",
            "local-db",
        }
    finally:
        await harness.stop()


async def test_a_scope_preference_selects_the_local_provider() -> None:
    harness = Harness()
    harness.install(tracked_provider("shared-db", "database"), entry_id="shared-db")
    research = harness.composition.child("research")
    research.install(tracked_provider("local-db", "database"), entry_id="local-db")
    research.install(consumer("agent", requires={"database": ">=1,<2"}), entry_id="agent")
    harness.prefer_provider("database", "local-db", scope="/research")
    try:
        result = await harness.start()

        assert set(result.plan.activation_order) == {"shared-db", "local-db", "agent"}
        explanation = harness.diagnostics.explain_requirement("agent", "database")
        assert explanation is not None
        assert explanation.selected is not None
        assert explanation.selected["provider_entry_id"] == "local-db"
        assert explanation.selected["origin"] == "local"
        assert explanation.reason == "explicit_preference"
    finally:
        await harness.stop()


# ---------------------------------------------------------------- immutability


async def test_published_scope_tree_is_deeply_immutable() -> None:
    harness = Harness()
    harness.composition.child("research")
    try:
        await harness.start()
        generation = harness.current_generation
        assert generation is not None
        resolved = generation.scopes.get("/research")
        assert resolved is not None

        with pytest.raises(dataclasses.FrozenInstanceError):
            resolved.name = "other"  # type: ignore[misc]
        with pytest.raises(TypeError):
            resolved.providers["database"] = ("x",)  # type: ignore[index]
        with pytest.raises(dataclasses.FrozenInstanceError):
            generation.scopes.scopes[0].entries = ()  # type: ignore[misc]
    finally:
        await harness.stop()


async def test_scope_affecting_changes_publish_a_new_generation() -> None:
    harness = Harness()
    harness.install(tracked_provider("postgres", "database"), entry_id="postgres")
    try:
        await harness.start()
        first = harness.current_generation
        assert first is not None
        assert first.scopes.paths() == (ROOT_PATH,)

        research = harness.composition.child("research")

        # Desired state changed; the published generation is untouched.
        assert harness.current_generation is first
        assert first.scopes.paths() == (ROOT_PATH,)

        result = await harness.reconcile()
        assert result.generation_id != first.generation_id
        assert generation_of(harness).scopes.paths() == (ROOT_PATH, "/research")
        # Topology changed, so the mount was reused rather than duplicated.
        assert result.reused == ("postgres",)

        # Adding an entry to the new scope is likewise invisible until published.
        research.install(tracked_provider("search", "search"), entry_id="search")
        assert harness.current_generation is not first
        assert scope_of(harness, "/research").entries == ()
        await harness.reconcile()
        assert scope_of(harness, "/research").entries == ("search",)
        assert mounted(harness, "search").state.value == "active"
    finally:
        await harness.stop()


async def test_a_narrowed_view_alone_publishes_a_new_generation() -> None:
    harness = Harness()
    harness.install(tracked_provider("model", "model"), entry_id="model")
    research = harness.composition.child("research")
    try:
        await harness.start()
        first = harness.current_generation
        assert first is not None

        research.restrict(MODEL)

        await harness.reconcile()
        assert generation_of(harness).generation_id != first.generation_id
        assert scope_of(harness, "/research").capabilities == ("model",)
        # The instance is unchanged: only the visibility view moved.
        assert mounted(harness, "model").generation_refs == 1
    finally:
        await harness.stop()


def test_desired_state_scopes_are_mutable_control_plane_objects() -> None:
    harness = Harness()
    scope = harness.composition.child("research", metadata={"team": "research"})
    scope.require(DATABASE)
    scope.restrict(MODEL)

    assert isinstance(scope, CompositionScope)
    assert scope.is_root is False
    assert harness.composition.root.is_root is True
    assert scope.requirements[0].name == "database"
    assert scope.allows(MODEL) is True
    assert scope.allows(DATABASE) is False
    assert scope.metadata == {"team": "research"}

    scope.drop_requirement(DATABASE)
    scope.unrestrict()
    assert scope.requirements == ()
    assert scope.capabilities is None


def test_scope_removal_detaches_the_subtree_and_uninstalls_entries() -> None:
    harness = Harness()
    tenant = harness.composition.child("tenant")
    tenant.install(tracked_provider("db", "database"), entry_id="db")
    tenant.child("research")

    removed = harness.composition.remove("/tenant")

    assert removed == ("db",)
    assert harness.composition.paths() == (ROOT_PATH,)
    assert harness.plugin_registry.entry("db") is None


async def test_scope_metadata_never_reaches_the_snapshot() -> None:
    harness = Harness()
    harness.composition.child("research", metadata={"note": "internal-only"})
    try:
        await harness.start()
        generation = harness.current_generation
        assert generation is not None

        rendered = str(harness.snapshot_for(generation).to_dict())

        assert "internal-only" not in rendered
        assert harness.diagnostics.explain_scope("/research").to_dict()["metadata"] == {
            "note": "internal-only"
        }
    finally:
        await harness.stop()


def test_scope_tree_root_only_is_a_valid_published_shape() -> None:
    tree = ScopeTree.root_only()
    root = tree.root_scope()

    assert isinstance(root, ResolvedScope)
    assert root.children == ()
    assert root.visible == {}
    assert root.name == "root"
    assert root.path == ROOT_PATH
