"""The planning contract: stable vocabulary, deterministic versioned output,
machine-readable reasons, and no sensitive configuration (roadmap R030).
"""

from __future__ import annotations

import asyncio
import json

from tests.planning.support import db_provider, memory_consumer, notes_a, recall_b

from chassis import Harness
from chassis.persistence.formats import PLAN_FORMAT_VERSION
from chassis.planning import ActionKind, GenerationImpact, ReasonCode


def test_action_and_reason_codes_are_the_documented_contract() -> None:
    """The vocabulary is closed and its values are the API."""

    assert sorted(kind.value for kind in ActionKind) == [
        "add",
        "no-op",
        "publish",
        "rebuild",
        "reject",
        "remove",
        "replace",
        "reuse",
    ]
    assert sorted(impact.value for impact in GenerationImpact) == ["new_generation", "none"]
    assert sorted(reason.value for reason in ReasonCode) == [
        "capability_contract_changed",
        "composition_changed",
        "composition_unchanged",
        "configuration_changed",
        "dependency_changed",
        "entry_added",
        "entry_removed",
        "implementation_changed",
        "instance_inactive",
        "pending_dependency",
        "plugin_cycle",
        "requirement_ambiguous",
        "requirement_unsatisfied",
        "revision_changed",
        "scope_visibility_changed",
        "semantic_identity_unchanged",
    ]


def test_preview_output_is_versioned_and_deterministic() -> None:
    harness = Harness()
    harness.install(db_provider, entry_id="orders-db")
    harness.install(memory_consumer, entry_id="memory")

    first = harness.preview().to_dict()
    second = harness.preview().to_dict()

    assert first == second
    assert json.loads(json.dumps(first, sort_keys=True)) == first
    assert first["format_version"] == PLAN_FORMAT_VERSION == 1
    assert first["chassis_version"]
    assert [action["entry_id"] for action in first["actions"]] == [
        "memory",
        "orders-db",
        None,
    ]
    assert first["actions"][-1]["action"] == "publish"


def test_actions_name_configuration_by_key_never_by_value() -> None:
    secret = "sk-live-9f8e7d6c5b4a"
    harness = Harness()
    harness.install(
        db_provider,
        entry_id="orders-db",
        config={"api_key": secret, "endpoint": f"https://example.test/{secret}"},
    )

    payload = harness.preview().to_dict()
    rendered = json.dumps(payload, sort_keys=True)

    action = next(item for item in payload["actions"] if item["entry_id"] == "orders-db")
    assert action["config_keys"] == ["api_key", "endpoint"]
    assert secret not in rendered
    assert "example.test" not in rendered


def test_an_unsatisfied_requirement_is_reported_with_machine_readable_detail() -> None:
    harness = Harness()
    harness.install(memory_consumer, entry_id="memory")

    payload = harness.preview().to_dict()

    action = next(item for item in payload["actions"] if item["entry_id"] == "memory")
    assert action["action"] == "reject"
    assert action["reasons"] == ["requirement_unsatisfied"]
    assert action["generation_impact"] == "none"
    assert action["validation_failures"] == [{"code": "no_provider", "detail": "database <2,>=1"}]


def test_an_ambiguous_requirement_names_candidates_and_the_preference() -> None:
    harness = Harness()
    harness.install(db_provider, entry_id="db-a")
    harness.install(db_provider, entry_id="db-b")
    harness.install(memory_consumer, entry_id="memory")

    ambiguous = harness.preview().to_dict()
    action = next(item for item in ambiguous["actions"] if item["entry_id"] == "memory")
    assert action["action"] == "reject"
    assert action["reasons"] == ["requirement_ambiguous"]
    assert action["ambiguities"] == [
        {
            "consumer": "memory",
            "capability": "database",
            "requirement": "database <2,>=1",
            "candidates": ["db-a", "db-b"],
            "preference": None,
        }
    ]

    harness.prefer_provider("database", "db-b", consumer="memory")
    resolved = harness.preview().to_dict()
    action = next(item for item in resolved["actions"] if item["entry_id"] == "memory")
    assert action["action"] in {"add", "rebuild", "reuse"}
    assert "requirement_ambiguous" not in action["reasons"]


def test_a_dependency_cycle_is_reported_not_guessed() -> None:
    harness = Harness()
    harness.install(notes_a, entry_id="notes")
    harness.install(recall_b, entry_id="recall")

    payload = harness.preview().to_dict()

    assert len(payload["cycles"]) == 1
    for entry_id in ("notes", "recall"):
        action = next(item for item in payload["actions"] if item["entry_id"] == entry_id)
        assert action["action"] == "reject"
        assert "plugin_cycle" in action["reasons"]
        assert any(failure["code"] == "plugin_cycle" for failure in action["validation_failures"])


def test_expected_reuse_and_generation_impact_are_declared_per_action() -> None:
    harness = Harness()
    harness.install(db_provider, entry_id="orders-db")
    planned = harness.preview().to_dict()
    add = next(item for item in planned["actions"] if item["entry_id"] == "orders-db")
    assert add["action"] == "add"
    assert add["expected_reuse"] is False
    assert add["instance_id"] is None
    assert add["generation_impact"] == "new_generation"

    asyncio.run(harness.start())
    try:
        stable = harness.preview().to_dict()
        reuse = next(item for item in stable["actions"] if item["entry_id"] == "orders-db")
        assert reuse["action"] == "reuse"
        assert reuse["reasons"] == ["semantic_identity_unchanged"]
        assert reuse["expected_reuse"] is True
        assert reuse["instance_id"] is not None
        assert reuse["generation_impact"] == "none"
        assert stable["actions"][-1]["action"] == "no-op"
        assert stable["would_publish"] is False
    finally:
        asyncio.run(harness.stop())
