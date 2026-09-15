from __future__ import annotations

import pytest
from packaging.version import Version

from chassis.capabilities.keys import MODEL, CapabilityKey, CapabilityRequirement
from chassis.capabilities.registry import CapabilityRegistration
from chassis.capabilities.snapshot import CapabilitySnapshot
from chassis.core.errors import (
    CapabilityAmbiguous,
    CapabilityNotFound,
    CapabilityVersionMismatch,
)


def registration(
    provider: str,
    capability: str,
    version: str,
) -> CapabilityRegistration:
    return CapabilityRegistration(
        registration_id=f"cap_{provider}",
        key=CapabilityKey.from_version(capability, version),
        version=Version(version),
        value=f"provider:{provider}",
        provider_id=f"plugin_{provider}",
        provider_name=provider,
        scope_id=f"scope_{provider}",
    )


def test_snapshot_orders_registrations_deterministically() -> None:
    snapshot = CapabilitySnapshot.from_registrations(
        "gen_0001",
        [
            registration("zeta", "model", "1.0.0"),
            registration("alpha", "model", "1.0.0"),
        ],
    )

    assert [item.provider_name for item in snapshot.registrations] == ["alpha", "zeta"]
    assert len(snapshot) == 2


def test_snapshot_requires_a_capability_or_raises() -> None:
    snapshot = CapabilitySnapshot.from_registrations(
        "gen_0001", [registration("a", "model", "1.0.0")]
    )

    assert snapshot.require(MODEL) == "provider:a"
    assert snapshot.require("model") == "provider:a"
    assert MODEL in snapshot

    with pytest.raises(CapabilityNotFound):
        snapshot.require("database")


def test_snapshot_reports_a_contract_generation_mismatch() -> None:
    snapshot = CapabilitySnapshot.from_registrations(
        "gen_0001", [registration("a", "model", "2.0.0")]
    )

    with pytest.raises(CapabilityVersionMismatch) as excinfo:
        snapshot.require(MODEL)

    assert excinfo.value.context["registered"] == "a@2.0.0"
    assert snapshot.providers(MODEL) == ()


def test_snapshot_refuses_to_pick_between_two_providers() -> None:
    snapshot = CapabilitySnapshot.from_registrations(
        "gen_0001",
        [registration("a", "model", "1.0.0"), registration("b", "model", "1.1.0")],
    )

    with pytest.raises(CapabilityAmbiguous) as excinfo:
        snapshot.require(MODEL)

    assert excinfo.value.context["providers"] == ["plugin_a", "plugin_b"]


def test_snapshot_resolution_matches_requirements() -> None:
    snapshot = CapabilitySnapshot.from_registrations(
        "gen_0001",
        [registration("a", "database", "1.4.0"), registration("b", "database", "2.0.0")],
    )

    resolved = snapshot.resolve(CapabilityRequirement.parse("database", ">=1,<2"))
    assert resolved.satisfied
    assert resolved.selected is not None and resolved.selected.provider_name == "a"

    mismatched = snapshot.resolve(CapabilityRequirement.parse("database", ">=3,<4"))
    assert mismatched.status == "version_mismatch"

    missing = snapshot.resolve(CapabilityRequirement.parse("memory"))
    assert missing.status == "no_provider"


def test_snapshot_resolution_reports_ambiguity_and_accepts_a_preference() -> None:
    snapshot = CapabilitySnapshot.from_registrations(
        "gen_0001",
        [registration("a", "database", "1.4.0"), registration("b", "database", "1.5.0")],
    )
    requirement = CapabilityRequirement.parse("database", ">=1,<2")

    ambiguous = snapshot.resolve(requirement)
    assert ambiguous.status == "ambiguous"
    assert [item.provider_name for item in ambiguous.candidates] == ["a", "b"]

    preferred = snapshot.resolve(requirement, prefer=["plugin_b"])
    assert preferred.satisfied
    assert preferred.selected is not None and preferred.selected.provider_name == "b"


def test_snapshot_versions_are_grouped_and_sorted() -> None:
    snapshot = CapabilitySnapshot.from_registrations(
        "gen_0001",
        [
            registration("a", "database", "1.4.0"),
            registration("b", "database", "1.2.0"),
            registration("c", "model", "1.0.0"),
        ],
    )

    assert snapshot.versions() == {"database": ("1.2.0", "1.4.0"), "model": ("1.0.0",)}


def test_snapshot_diagnostics_exclude_provider_payloads() -> None:
    snapshot = CapabilitySnapshot.from_registrations(
        "gen_0001", [registration("a", "model", "1.0.0")]
    )

    payload = snapshot.to_dict()

    assert payload["generation_id"] == "gen_0001"
    assert payload["registrations"][0]["provider_name"] == "a"
    assert "provider:a" not in str(payload)


def test_snapshot_is_immutable() -> None:
    snapshot = CapabilitySnapshot.from_registrations("gen_0001", [])

    with pytest.raises(AttributeError):
        snapshot.generation_id = "gen_0002"  # type: ignore[misc]
