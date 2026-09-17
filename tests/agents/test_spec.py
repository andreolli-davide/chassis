"""AgentSpec: an immutable, engine-neutral description of composition intent."""

from __future__ import annotations

from typing import Any

import pytest

from chassis.agent_spec import AgentRevision, AgentSpec, composition_payload
from chassis.core.errors import ConfigurationError


def test_minimal_spec_has_deterministic_identity_and_default_scope() -> None:
    spec = AgentSpec(name="finance", revision="17")

    assert spec.identity == "finance@17"
    assert spec.scope_path == "/agents/finance"
    assert spec.runtime_ref is None
    assert spec.capabilities is None
    assert spec.tools is None
    assert spec.requires == {}


def test_spec_rejects_invalid_name_and_revision() -> None:
    with pytest.raises(ConfigurationError):
        AgentSpec(name="")
    with pytest.raises(ConfigurationError):
        AgentSpec(name=" finance")
    with pytest.raises(ConfigurationError):
        AgentSpec(name="fin/ance")
    with pytest.raises(ConfigurationError):
        AgentSpec(name="finance", revision="")
    with pytest.raises(ConfigurationError):
        AgentSpec(name="finance", revision="17 ")


def test_spec_rejects_invalid_scope_and_requirements() -> None:
    with pytest.raises(ConfigurationError):
        AgentSpec(name="finance", scope="relative/path")
    with pytest.raises(ConfigurationError):
        AgentSpec(name="finance", scope="/agents/../root")
    with pytest.raises(ConfigurationError):
        AgentSpec(name="finance", requires={"model": "not-a-specifier"})
    with pytest.raises(ConfigurationError):
        AgentSpec(name="finance", requires={"": ">=1"})


def test_spec_rejects_invalid_plugin_configuration() -> None:
    with pytest.raises(ConfigurationError):
        AgentSpec(name="finance", plugins={"ledger": 7})


def test_collections_are_frozen_and_do_not_alias_the_author() -> None:
    author_config: dict[str, Any] = {"pool": {"size": 4}, "hosts": ["a", "b"]}
    author_metadata: dict[str, Any] = {"team": "finance"}
    spec = AgentSpec(
        name="finance",
        capabilities=["model", "database"],
        tools=["ledger"],
        requires={"database": ">=1,<2"},
        plugins={"ledger": author_config},
        metadata=author_metadata,
    )

    # Mutating the authoring objects afterwards must not change the spec.
    author_config["pool"]["size"] = 99
    author_config["hosts"].append("c")
    author_metadata["team"] = "other"

    assert spec.plugins["ledger"]["pool"]["size"] == 4
    assert tuple(spec.plugins["ledger"]["hosts"]) == ("a", "b")
    assert spec.metadata["team"] == "finance"

    assert isinstance(spec.capabilities, frozenset)
    assert isinstance(spec.tools, frozenset)
    with pytest.raises(TypeError):
        spec.requires["database"] = ">=2"  # type: ignore[index]
    with pytest.raises(TypeError):
        spec.metadata["team"] = "x"  # type: ignore[index]
    with pytest.raises(TypeError):
        spec.plugins["ledger"]["pool"] = {}  # type: ignore[index]


def test_spec_accepts_a_plugin_type_programmatically() -> None:
    class Ledger:
        pass

    spec = AgentSpec(name="finance", plugins={"ledger": Ledger})

    assert spec.plugins["ledger"] is Ledger
    assert spec.to_dict()["plugins"] == {"ledger": "<Ledger>"}


def test_to_dict_reports_configuration_by_key_only() -> None:
    spec = AgentSpec(
        name="finance",
        plugins={"ledger": {"api_key": "super-secret", "region": "eu"}},
        metadata={"token": "also-secret"},
    )

    rendered = spec.to_dict()

    assert rendered["plugins"] == {"ledger": ["api_key", "region"]}
    assert rendered["metadata_keys"] == ["token"]
    assert "super-secret" not in str(rendered)
    assert "also-secret" not in str(rendered)


def test_from_dict_applies_the_same_validation() -> None:
    spec = AgentSpec.from_dict(
        {
            "name": "research",
            "revision": "3",
            "capabilities": ["model"],
            "requires": {"database": ">=1,<2"},
            "plugins": {"search": {"endpoint": "https://example"}},
        }
    )

    assert spec.identity == "research@3"
    assert spec.capabilities == frozenset({"model"})
    assert spec.plugins["search"]["endpoint"] == "https://example"

    with pytest.raises(ConfigurationError):
        AgentSpec.from_dict({"name": "x", "unknown": 1})
    with pytest.raises(ConfigurationError):
        AgentSpec.from_dict({"revision": "1"})


def test_from_dict_roundtrips_the_structure_to_dict_reports() -> None:
    # to_dict() is diagnostic-safe: metadata and plugin configuration are reported
    # by key, so a roundtrip preserves structure, not secret values.
    spec = AgentSpec(
        name="support",
        revision="2",
        scope="/support",
        runtime_ref="support-graph",
        capabilities=["model", "database"],
        requires={"database": ">=1,<2"},
        tools=["ticket-lookup"],
        profile="reasoning",
        plugins={"tickets": None},
    )

    rebuilt = AgentSpec.from_dict(spec.to_dict())

    assert rebuilt == spec


def test_composition_payload_covers_every_composition_affecting_input() -> None:
    spec = AgentSpec(
        name="finance",
        revision="18",
        tools=["web"],
        profile="reasoning",
        plugins={"ledger": {"pool": 2}},
    )

    payload = composition_payload(spec)

    assert payload["tools"] == ["web"]
    assert payload["profile"] == "reasoning"
    assert payload["plugins"] == {"ledger": {"pool": 2}}


def test_revision_records_its_materialization_without_serializing_secrets() -> None:
    spec = AgentSpec(name="finance", revision="17", plugins={"ledger": {"api_key": "x"}})
    revision = AgentRevision(spec=spec, scope="/agents/finance", entries=("finance.ledger",))

    rendered = revision.to_dict()

    assert rendered["identity"] == "finance@17"
    assert rendered["scope"] == "/agents/finance"
    assert "x" not in str(rendered)
