"""Scope-aware resolution: visibility, ambiguity, provenance, scope requirements.

Resolution is a pure function, so most of this file drives the resolver directly;
the harness-level shape of the same rules is covered by ``test_scopes.py``.
"""

from __future__ import annotations

import pytest

from chassis.capabilities.keys import CapabilityRequirement
from chassis.composition import ScopeSpec
from chassis.core.errors import ConfigurationError
from chassis.plugins.manifest import PluginManifest
from chassis.plugins.resolver import DependencyResolver, PluginCandidate


def candidate(
    entry_id: str,
    *,
    scope: str = "/",
    provides: dict[str, str] | None = None,
    requires: dict[str, str] | None = None,
    optional: dict[str, str] | None = None,
) -> PluginCandidate:
    return PluginCandidate(
        entry_id=entry_id,
        manifest=PluginManifest(
            name=entry_id,
            version="1.0.0",
            provides=dict(provides or {}),
            requires=dict(requires or {}),
            optional=dict(optional or {}),
        ),
        scope=scope,
    )


ROOT = ScopeSpec(path="/", name="root", parent=None)
TENANT = ScopeSpec(path="/tenant", name="tenant", parent="/")
RESEARCH = ScopeSpec(path="/tenant/research", name="research", parent="/tenant")
FINANCE = ScopeSpec(path="/tenant/finance", name="finance", parent="/tenant")
SCOPES = (ROOT, TENANT, RESEARCH, FINANCE)


def test_plan_is_deterministic_regardless_of_input_order_across_scopes() -> None:
    db = candidate("db", provides={"database": "1.0.0"})
    memory = candidate(
        "memory",
        scope="/tenant/research",
        provides={"memory": "1.0.0"},
        requires={"database": ">=1,<2"},
    )
    resolver = DependencyResolver()

    first = resolver.resolve([memory, db], scopes=SCOPES)
    second = resolver.resolve([db, memory], scopes=SCOPES)

    assert first == second
    assert first.activation_order == ("db", "memory")
    assert first.edges == (("db", "memory"),)


def test_child_scope_inherits_from_every_ancestor() -> None:
    root_db = candidate("root-db", provides={"database": "1.0.0"})
    tenant_model = candidate("tenant-model", scope="/tenant", provides={"model": "1.0.0"})
    agent = candidate(
        "agent",
        scope="/tenant/research",
        requires={"database": ">=1,<2", "model": ">=1,<2"},
    )

    plan = DependencyResolver().resolve([root_db, tenant_model, agent], scopes=SCOPES)

    assert plan.activation_order == ("root-db", "tenant-model", "agent")
    research = plan.scope_for("/tenant/research")
    assert research is not None
    assert research.inherited == {"database": ("root-db",), "model": ("tenant-model",)}
    assert research.providers == {}


def test_sibling_local_providers_are_not_candidates() -> None:
    finance_erp = candidate("finance-erp", scope="/tenant/finance", provides={"erp": "1.0.0"})
    research_agent = candidate("agent", scope="/tenant/research", requires={"erp": ">=1,<2"})

    plan = DependencyResolver().resolve([finance_erp, research_agent], scopes=SCOPES)

    assert plan.pending == ("agent",)
    agent_plan = plan.plan_for("agent")
    assert agent_plan is not None
    resolution = agent_plan.requirements[0]
    assert resolution.status == "not_visible"
    finance_assessment = [item for item in resolution.assessments if item.scope != "/"]
    assert finance_assessment[0].rejection == "not_visible"
    assert finance_assessment[0].visible is False


def test_ambiguous_across_local_and_inherited_providers_and_preference_precedence() -> None:
    inherited = candidate("inherited-db", provides={"database": "1.0.0"})
    local = candidate("local-db", scope="/tenant", provides={"database": "1.0.0"})
    consumer = candidate("agent", scope="/tenant", requires={"database": ">=1,<2"})
    resolver = DependencyResolver()

    ambiguous = resolver.resolve([inherited, local, consumer], scopes=SCOPES)
    assert ambiguous.pending == ("agent",)
    assert ambiguous.plan_for("agent").requirements[0].status == "ambiguous"  # type: ignore[union-attr]

    by_scope = resolver.resolve(
        [inherited, local, consumer], scopes=SCOPES, prefer={"scope:/tenant:database": "local-db"}
    )
    resolution = by_scope.plan_for("agent").requirements[0]  # type: ignore[union-attr]
    assert resolution.provider_entry_id == "local-db"
    assert resolution.selection_reason == "explicit_preference"
    assert resolution.provider_origin == "local"

    # A consumer-specific preference beats a scope preference, which beats global.
    by_consumer = resolver.resolve(
        [inherited, local, consumer],
        scopes=SCOPES,
        prefer={
            "agent:database": "inherited-db",
            "scope:/tenant:database": "local-db",
            "database": "local-db",
        },
    )
    assert by_consumer.plan_for("agent").requirements[0].provider_entry_id == "inherited-db"  # type: ignore[union-attr]

    global_only = resolver.resolve(
        [inherited, local, consumer], scopes=SCOPES, prefer={"database": "local-db"}
    )
    assert global_only.plan_for("agent").requirements[0].provider_entry_id == "local-db"  # type: ignore[union-attr]


def test_version_mismatch_is_reported_with_its_provenance() -> None:
    db = candidate("db", provides={"database": "2.5.0"})
    consumer = candidate("agent", requires={"database": ">=1,<2"})

    plan = DependencyResolver().resolve([db, consumer], scopes=SCOPES)

    assert plan.pending == ("agent",)
    resolution = plan.plan_for("agent").requirements[0]  # type: ignore[union-attr]
    assert resolution.status == "version_mismatch"
    assert resolution.assessments[0].eligible is False
    assert resolution.assessments[0].rejection == "contract_mismatch"


def test_version_mismatch_within_the_same_contract_generation() -> None:
    db = candidate("db", provides={"database": "1.2.0"})
    consumer = candidate("agent", requires={"database": ">=1.5,<2"})

    plan = DependencyResolver().resolve([db, consumer], scopes=SCOPES)

    resolution = plan.plan_for("agent").requirements[0]  # type: ignore[union-attr]
    assert resolution.status == "version_mismatch"
    assert resolution.assessments[0].rejection == "version_mismatch"


def test_a_pending_provider_is_reported_as_such() -> None:
    # The provider is visible but cannot activate itself, so its consumer must not
    # pretend it is satisfiable.
    provider = candidate("provider", requires={"missing": ">=1,<2"}, provides={"memory": "1.0.0"})
    consumer = candidate("agent", requires={"memory": ">=1,<2"})

    plan = DependencyResolver().resolve([provider, consumer], scopes=SCOPES)

    assert plan.pending == ("agent", "provider")
    resolution = plan.plan_for("agent").requirements[0]  # type: ignore[union-attr]
    assert resolution.status == "provider_pending"
    assert resolution.assessments[0].rejection == "provider_pending"


def test_unresolved_requirement_has_an_authoritative_reason() -> None:
    consumer = candidate("agent", requires={"database": ">=1,<2"})

    plan = DependencyResolver().resolve([consumer], scopes=SCOPES)

    resolution = plan.plan_for("agent").requirements[0]  # type: ignore[union-attr]
    assert resolution.status == "no_provider"
    assert "no provider for 'database'" in resolution.explain


def test_cycle_detection_ignores_edges_visibility_forbids() -> None:
    # `a` in research declares a dependency on `b`, and `b` in finance declares one
    # on `a`. Neither can see the other, so there is no cycle.
    a = candidate("a", scope="/tenant/research", provides={"x": "1.0.0"}, requires={"y": ">=1,<2"})
    b = candidate("b", scope="/tenant/finance", provides={"y": "1.0.0"}, requires={"x": ">=1,<2"})

    plan = DependencyResolver().resolve([a, b], scopes=SCOPES)

    assert plan.cycles == ()
    assert plan.pending == ("a", "b")


def test_a_cycle_inside_a_child_scope_is_detected() -> None:
    # Mutual visibility implies the same scope: an ancestor sees its own providers
    # and its ancestors', a descendant sees none of its own; so a cycle can only
    # exist among providers a consumer can reciprocally observe.
    a = candidate("a", scope="/tenant", provides={"x": "1.0.0"}, requires={"y": ">=1,<2"})
    b = candidate("b", scope="/tenant", provides={"y": "1.0.0"}, requires={"x": ">=1,<2"})

    plan = DependencyResolver().resolve([a, b], scopes=SCOPES)

    assert plan.cycles == (("a", "b"),)
    assert plan.pending == ("a", "b")


def test_selected_provider_provenance_names_origin_and_reason() -> None:
    db = candidate("db", provides={"database": "1.3.0"})
    memory = candidate(
        "memory",
        scope="/tenant/research",
        provides={"memory": "1.0.0"},
        requires={"database": ">=1,<2"},
    )

    plan = DependencyResolver().resolve([db, memory], scopes=SCOPES)

    resolution = plan.plan_for("memory").requirements[0]  # type: ignore[union-attr]
    assert resolution.status == "resolved"
    assert resolution.selection_reason == "only_eligible"
    assert resolution.provider_origin == "inherited"
    assert resolution.provider_scope == "/"
    assert resolution.consumer == "memory"
    assert resolution.consumer_kind == "plugin"
    assert resolution.assessments[0].selected is True
    assert resolution.assessments[0].origin == "inherited"


def test_scope_local_requirements_are_resolved_and_do_not_gate_plugins() -> None:
    db = candidate("db", provides={"database": "1.0.0"})
    plugin = candidate("worker", scope="/tenant", provides={"work": "1.0.0"})
    specs = (
        ROOT,
        ScopeSpec(
            path="/tenant",
            name="tenant",
            parent="/",
            requirements=(CapabilityRequirement.parse("database", ">=1,<2"),),
        ),
    )

    plan = DependencyResolver().resolve([db, plugin], scopes=specs)

    tenant = plan.scope_for("/tenant")
    assert tenant is not None
    assert tenant.entries == ("worker",)
    assert plugin.entry_id in tenant.order
    assert [item.status for item in tenant.requirements] == ["resolved"]
    assert tenant.requirements[0].consumer == "/tenant"
    assert tenant.requirements[0].consumer_kind == "scope"
    assert tenant.requirements[0].provider_entry_id == "db"


def test_an_unsatisfied_scope_requirement_does_not_remove_its_plugins() -> None:
    specs = (
        ROOT,
        ScopeSpec(
            path="/tenant",
            name="tenant",
            parent="/",
            requirements=(CapabilityRequirement.parse("database", ">=1,<2"),),
        ),
    )
    plugin = candidate("worker", scope="/tenant", provides={"work": "1.0.0"})

    plan = DependencyResolver().resolve([plugin], scopes=specs)

    tenant = plan.scope_for("/tenant")
    assert tenant is not None
    assert tenant.requirements[0].status == "no_provider"
    assert plan.activation_order == ("worker",)


def test_capability_view_filters_local_and_inherited_providers_alike() -> None:
    db = candidate("db", provides={"database": "1.0.0"})
    scheduler = candidate("scheduler", provides={"scheduler": "1.0.0"})
    local = candidate("local-db", scope="/tenant", provides={"database": "1.0.0"})
    specs = (
        ROOT,
        ScopeSpec(
            path="/tenant",
            name="tenant",
            parent="/",
            capabilities=frozenset({"database"}),
        ),
    )

    plan = DependencyResolver().resolve([db, scheduler, local], scopes=specs)

    tenant = plan.scope_for("/tenant")
    assert tenant is not None
    assert tenant.capabilities == ("database",)
    assert set(tenant.visible) == {"database"}
    assert tenant.providers == {"database": ("local-db",)}
    assert tenant.inherited == {"database": ("db",)}


def test_unknown_and_duplicate_scope_paths_are_refused() -> None:
    resolver = DependencyResolver()
    orphan = candidate("orphan", scope="/missing", provides={"x": "1.0.0"})

    with pytest.raises(ConfigurationError):
        resolver.resolve([orphan], scopes=SCOPES)

    with pytest.raises(ConfigurationError):
        resolver.resolve([], scopes=(ROOT, ScopeSpec(path="/", name="root", parent=None)))


def test_a_plan_without_scopes_stays_flat() -> None:
    db = candidate("db", provides={"database": "1.0.0"})
    consumer = candidate("agent", requires={"database": ">=1,<2"})

    plan = DependencyResolver().resolve([db, consumer])

    assert plan.scope_for("/") is not None
    assert plan.scope_for("/").entries == ("agent", "db")  # type: ignore[union-attr]
    assert plan.activation_order == ("db", "agent")


def test_plan_to_dict_carries_scopes_and_provenance() -> None:
    db = candidate("db", provides={"database": "1.0.0"})
    memory = candidate("memory", scope="/tenant", requires={"database": ">=1,<2"})

    payload = DependencyResolver().resolve([db, memory], scopes=SCOPES).to_dict()

    paths = [scope["path"] for scope in payload["scopes"]]
    assert paths == ["/", "/tenant", "/tenant/finance", "/tenant/research"]
    tenant = next(scope for scope in payload["scopes"] if scope["path"] == "/tenant")
    assert tenant["provenance"][0]["consumer"] == "memory"
    assert tenant["provenance"][0]["provider_origin"] == "inherited"
