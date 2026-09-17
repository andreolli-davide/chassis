from __future__ import annotations

import pytest
from pydantic import ValidationError

from chassis.capabilities import CapabilityKey
from chassis.plugins.manifest import PluginManifest


def test_manifest_parses_capabilities_deterministically() -> None:
    manifest = PluginManifest(
        name="memory",
        version="1.2.0",
        provides={"memory": "1.2.0"},
        requires={"database": ">=1,<2"},
        optional={"vector": ">=2"},
        permissions=["database.query", "network.fetch"],
    )

    assert manifest.identity == "memory@1.2.0"
    assert manifest.parsed_version.major == 1
    assert manifest.provided_keys() == (CapabilityKey("memory", "1"),)

    (requirement,) = manifest.required_capabilities()
    assert requirement.key == CapabilityKey("database", "1")
    assert requirement.optional is False

    (optional,) = manifest.optional_capabilities()
    assert optional.key == CapabilityKey("vector", "2")
    assert optional.optional is True

    assert manifest.to_dict()["permissions"] == ["database.query", "network.fetch"]


def test_provided_keys_are_derived_from_the_implementation_version() -> None:
    manifest = PluginManifest(name="db", version="1.0.0", provides={"database": "3.4.1"})

    assert manifest.provided_keys() == (CapabilityKey("database", "3"),)


def test_manifest_rejects_invalid_version() -> None:
    with pytest.raises(ValidationError):
        PluginManifest(name="bad", version="not-a-version")


def test_manifest_rejects_invalid_requirement() -> None:
    with pytest.raises(ValidationError):
        PluginManifest(name="bad", version="1.0.0", requires={"database": "nonsense"})
    with pytest.raises(ValidationError):
        PluginManifest(name="bad", version="1.0.0", provides={"database": "nonsense"})
    with pytest.raises(ValidationError):
        PluginManifest(name="bad", version="1.0.0", optional={"database": "nonsense"})


def test_manifest_rejects_empty_name() -> None:
    with pytest.raises(ValidationError):
        PluginManifest(name="", version="1.0.0")


def test_manifest_is_immutable_and_rejects_unknown_fields() -> None:
    manifest = PluginManifest(name="x", version="1.0.0")

    with pytest.raises(ValidationError):
        manifest.name = "y"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        PluginManifest(name="x", version="1.0.0", unknown_field=True)  # type: ignore[call-arg]


def test_manifest_serialization_is_key_sorted() -> None:
    manifest = PluginManifest(
        name="x",
        version="1.0.0",
        provides={"b": "1.0.0", "a": "1.0.0"},
        metadata={"z": 1, "a": 2},
    )

    payload = manifest.to_dict()

    assert list(payload["provides"]) == ["a", "b"]
    assert list(payload["metadata"]) == ["a", "z"]


def test_implementation_revision_is_optional_and_validated() -> None:
    default = PluginManifest(name="x", version="1.0.0")

    assert default.implementation_revision is None
    assert default.to_dict()["implementation_revision"] is None

    declared = PluginManifest(name="x", version="1.0.0", implementation_revision="build-7")

    assert declared.implementation_revision == "build-7"
    assert declared.to_dict()["implementation_revision"] == "build-7"

    with pytest.raises(ValidationError):
        PluginManifest(name="x", version="1.0.0", implementation_revision="  ")
