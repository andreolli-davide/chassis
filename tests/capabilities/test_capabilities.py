from __future__ import annotations

import pytest
from packaging.version import Version

from chassis.capabilities import (
    DATABASE,
    MODEL,
    CapabilityKey,
    CapabilityRegistry,
    CapabilityRequirement,
    ScopedCapabilities,
    specifier_major,
)
from chassis.capabilities.keys import parse_specifier
from chassis.core.errors import ConfigurationError
from chassis.core.scope import Scope


def test_key_from_version_uses_major_as_contract_generation() -> None:
    assert CapabilityKey.from_version("database", "2.1.0") == CapabilityKey("database", "2")
    assert CapabilityKey.from_version("database", Version("10.0")) == CapabilityKey(
        "database", "10"
    )
    assert str(CapabilityKey("model", "1")) == "model@1"


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        (">=1,<2", "1"),
        (">=2,<3", "2"),
        ("==2.1.*", "2"),
        ("~=3.2", "3"),
        ("1.2.0", "1"),
        (">=1.5,<2.0.0", "1"),
        (">=1,<2,!=1.5.0", "1"),
        (">=3,<4", "3"),
        (">1.9,<3", None),
        (">=1", None),
        ("", None),
        ("<3", None),
        ("!=2", None),
    ],
)
def test_requirement_derives_contract_generation(requirement: str, expected: str | None) -> None:
    assert specifier_major(parse_specifier(requirement, capability="x")) == expected
    parsed = CapabilityRequirement.parse("x", requirement)
    assert parsed.key.api_version == (expected or "")
    assert parsed.is_generation_agnostic is (expected is None)


def test_bare_version_requirement_is_normalized_to_exact_match() -> None:
    requirement = CapabilityRequirement.parse("tools", "1.2.0")
    assert str(requirement.specifier) == "==1.2.0"


def test_invalid_requirement_and_version_are_reported() -> None:
    with pytest.raises(ConfigurationError):
        CapabilityRequirement.parse("tools", "not a version")
    with pytest.raises(ConfigurationError):
        CapabilityKey.from_version("tools", "not-a-version")
    with pytest.raises(ConfigurationError):
        CapabilityKey("", "1")
    with pytest.raises(ConfigurationError):
        CapabilityKey("model", "v1")


def test_registration_is_owned_by_the_providing_scope() -> None:
    registry = CapabilityRegistry()
    scope = Scope("provider")
    registry.provide(scope=scope, key=MODEL, value=object(), version="1.2.0", provider_id="p1")

    assert len(registry) == 1
    assert scope.effects[0].kind == "capability"


async def test_closing_scope_withdraws_registration() -> None:
    registry = CapabilityRegistry()
    scope = Scope("provider")
    registry.provide(scope=scope, key=MODEL, value=object(), version="1.2.0", provider_id="p1")

    await scope.aclose()

    assert len(registry) == 0


def test_scoped_capabilities_facade_uses_bound_scope() -> None:
    registry = CapabilityRegistry()
    scope = Scope("provider")
    capabilities = ScopedCapabilities(registry, scope, "plugin_instance", "openai")

    registration = capabilities.provide(MODEL, object(), version="1.4.0")

    assert registration.key == MODEL
    assert registration.version == Version("1.4.0")
    assert registration.scope_id == scope.id
    assert registration.provider_id == "plugin_instance"
    assert registration.provider_name == "openai"


def test_providing_version_outside_contract_generation_is_rejected() -> None:
    registry = CapabilityRegistry()
    scope = Scope("provider")

    with pytest.raises(ConfigurationError):
        registry.provide(scope=scope, key=MODEL, value=object(), version="2.0.0", provider_id="p1")


def test_resolution_distinguishes_missing_from_incompatible() -> None:
    registry = CapabilityRegistry()
    scope = Scope("provider")
    database_v2 = CapabilityKey.from_version("database", "2.1.0")
    registry.provide(
        scope=scope, key=database_v2, value=object(), version="2.1.0", provider_id="db2"
    )

    missing = registry.resolve(CapabilityRequirement.parse("memory", ">=1,<2"))
    assert missing.status == "no_provider"
    assert "no provider" in missing.explain()

    incompatible = registry.resolve(CapabilityRequirement.parse("database", ">=1,<2"))
    assert incompatible.status == "version_mismatch"
    assert incompatible.candidates == ()
    assert [reg.provider_id for reg in incompatible.same_name] == ["db2"]

    resolved = registry.resolve(CapabilityRequirement.parse("database", ">=2,<3"))
    assert resolved.satisfied
    assert resolved.selected is not None
    assert resolved.selected.provider_id == "db2"


def test_requirement_pins_the_contract_generation() -> None:
    registry = CapabilityRegistry()
    scope = Scope("provider")
    registry.provide(
        scope=scope,
        key=CapabilityKey.from_version("database", "3.0.0"),
        value=object(),
        version="3.0.0",
        provider_id="db3",
    )

    pinned = registry.resolve(CapabilityRequirement.parse("database", ">=3"))
    assert pinned.satisfied

    other_generation = registry.resolve(CapabilityRequirement.parse("database", ">=2,<3"))
    assert other_generation.status == "version_mismatch"


def test_generation_agnostic_requirement_matches_any_contract_generation() -> None:
    registry = CapabilityRegistry()
    scope = Scope("provider")
    registry.provide(
        scope=scope,
        key=CapabilityKey.from_version("database", "3.0.0"),
        value=object(),
        version="3.0.0",
        provider_id="db3",
    )

    resolution = registry.resolve(CapabilityRequirement.parse("database"))

    assert resolution.satisfied
    assert resolution.selected is not None
    assert resolution.selected.key.api_version == "3"


def test_ambiguous_provider_selection_is_reported_deterministically() -> None:
    registry = CapabilityRegistry()
    scope_a = Scope("a")
    scope_b = Scope("b")
    registry.provide(scope=scope_a, key=MODEL, value="a", version="1.1.0", provider_id="pa")
    registry.provide(scope=scope_b, key=MODEL, value="b", version="1.2.0", provider_id="pb")

    requirement = CapabilityRequirement.parse("model", ">=1,<2")
    ambiguous = registry.resolve(requirement)
    assert ambiguous.status == "ambiguous"
    assert [reg.provider_id for reg in ambiguous.candidates] == ["pa", "pb"]
    assert "ambiguous" in ambiguous.explain()

    preferred = registry.resolve(requirement, prefer=["pb"])
    assert preferred.satisfied
    assert preferred.selected is not None and preferred.selected.provider_id == "pb"

    still_ambiguous = registry.resolve(requirement, prefer=["unknown"])
    assert still_ambiguous.status == "ambiguous"


def test_registration_order_is_deterministic() -> None:
    registry = CapabilityRegistry()
    scope = Scope("provider")
    registry.provide(
        scope=scope, key=MODEL, value="b", version="1.1.0", provider_id="pb", provider_name="b"
    )
    registry.provide(
        scope=scope, key=MODEL, value="a", version="1.0.0", provider_id="pa", provider_name="a"
    )

    assert [reg.provider_name for reg in registry.registrations()] == ["a", "b"]


def test_withdraw_provider_removes_all_its_registrations() -> None:
    registry = CapabilityRegistry()
    scope = Scope("provider")
    registry.provide(scope=scope, key=MODEL, value=object(), version="1.0.0", provider_id="p1")
    registry.provide(scope=scope, key=DATABASE, value=object(), version="1.0.0", provider_id="p1")
    registry.provide(scope=scope, key=DATABASE, value=object(), version="1.0.0", provider_id="p2")

    removed = registry.withdraw_provider("p1")

    assert len(removed) == 2
    assert [reg.provider_id for reg in registry.registrations()] == ["p2"]


def test_registration_diagnostics_exclude_provider_payload() -> None:
    registry = CapabilityRegistry()
    scope = Scope("provider")
    registry.provide(
        scope=scope, key=MODEL, value={"api_key": "sk-secret"}, version="1.0.0", provider_id="p1"
    )

    payload = registry.to_dict()["registrations"][0]

    assert payload["capability"] == "model@1"
    assert "value" not in payload
    assert "sk-secret" not in str(registry.to_dict())


def test_metadata_is_immutable() -> None:
    registry = CapabilityRegistry()
    scope = Scope("provider")
    source = {"tenant": "acme"}
    registration = registry.provide(
        scope=scope, key=MODEL, value=object(), version="1.0.0", provider_id="p1", metadata=source
    )

    source["tenant"] = "mutated"

    assert registration.metadata["tenant"] == "acme"
    with pytest.raises(TypeError):
        registration.metadata["tenant"] = "nope"  # type: ignore[index]
