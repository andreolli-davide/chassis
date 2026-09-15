"""Registry of capability providers.

The registry is a mutable index owned by the control plane. Each registration is
created *through a scope*, which means the scope owns it: closing the scope
withdraws the registration (invariant I1). Registries therefore never need
callers to write matching deregistration code.

Provider resolution is deterministic. Candidates are ordered by
``(provider_name, provider_id, registration_id)`` and an ambiguous requirement
is reported as such instead of resolving to whichever provider happened to be
registered first.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from packaging.version import Version

from chassis.capabilities.keys import (
    CapabilityKey,
    CapabilityRequirement,
    parse_version,
)
from chassis.core.errors import ConfigurationError
from chassis.core.scope import Scope

__all__ = [
    "CapabilityRegistration",
    "CapabilityRegistry",
    "CapabilityResolution",
    "ResolutionStatus",
    "ScopedCapabilities",
]

ResolutionStatus = Literal["resolved", "no_provider", "version_mismatch", "ambiguous"]


@dataclass(frozen=True, slots=True, eq=False)
class CapabilityRegistration:
    """A provider bound to one capability contract.

    ``value`` is the provider object handed to consumers. It never participates in
    equality, hashing, or serialization: diagnostics describe the registration,
    not the provider payload.
    """

    registration_id: str
    key: CapabilityKey
    version: Version
    value: object
    provider_id: str
    provider_name: str
    scope_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        """Diagnostic view. Provider payloads are deliberately excluded."""

        return {
            "registration_id": self.registration_id,
            "capability": str(self.key),
            "version": str(self.version),
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "scope_id": self.scope_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CapabilityResolution:
    """Outcome of resolving one requirement against the registry."""

    requirement: CapabilityRequirement
    status: ResolutionStatus
    selected: CapabilityRegistration | None
    candidates: tuple[CapabilityRegistration, ...]
    same_name: tuple[CapabilityRegistration, ...]

    @property
    def satisfied(self) -> bool:
        return self.status == "resolved"

    def explain(self) -> str:
        """Human-readable explanation, used by diagnostics."""

        requirement = str(self.requirement)
        if self.status == "resolved":
            assert self.selected is not None
            return (
                f"{requirement} resolved by {self.selected.provider_name} ({self.selected.version})"
            )
        if self.status == "no_provider":
            return f"{requirement}: no provider registered for {self.requirement.name}"
        if self.status == "version_mismatch":
            offered = ", ".join(f"{reg.provider_name}@{reg.version}" for reg in self.same_name)
            return f"{requirement}: no compatible provider; registered: {offered or 'none'}"
        selected = ", ".join(reg.provider_id for reg in self.candidates)
        return f"{requirement}: ambiguous provider selection among {selected}"


class CapabilityRegistry:
    """Mutable index of capability providers, used while composing a generation.

    Agent runs never read this object: they receive an immutable capability
    snapshot bound to their runtime generation.
    """

    def __init__(self) -> None:
        self._registrations: dict[str, CapabilityRegistration] = {}

    # ---------------------------------------------------------------- mutation

    def provide(
        self,
        *,
        scope: Scope,
        key: CapabilityKey,
        value: object,
        version: str | Version | None = None,
        provider_id: str,
        provider_name: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> CapabilityRegistration:
        """Register ``value`` as a provider of ``key``, owned by ``scope``.

        The registration is withdrawn automatically when ``scope`` closes.
        """

        scope.assert_open(f"provide capability {key}")
        if not key.api_version:
            raise ConfigurationError(
                "cannot provide a generation-agnostic capability key",
                capability=key.name,
            )
        parsed_version = _registration_version(key, version)
        registration = CapabilityRegistration(
            registration_id=f"cap_{uuid.uuid4().hex[:12]}",
            key=key,
            version=parsed_version,
            value=value,
            provider_id=provider_id,
            provider_name=provider_name or provider_id,
            scope_id=scope.id,
            metadata=dict(metadata or {}),
        )
        self._registrations[registration.registration_id] = registration
        scope.cleanup(
            f"capability {key} from {registration.provider_name}",
            self.withdraw,
            registration.registration_id,
            kind="capability",
        )
        return registration

    def withdraw(self, registration_id: str) -> bool:
        """Remove a registration. Returns whether it was present."""

        return self._registrations.pop(registration_id, None) is not None

    def withdraw_provider(self, provider_id: str) -> tuple[CapabilityRegistration, ...]:
        """Remove every registration owned by ``provider_id``."""

        removed = tuple(
            registration
            for registration in self._registrations.values()
            if registration.provider_id == provider_id
        )
        for registration in removed:
            self.withdraw(registration.registration_id)
        return removed

    def clear(self) -> None:
        self._registrations.clear()

    # ----------------------------------------------------------------- reading

    def get(self, registration_id: str) -> CapabilityRegistration | None:
        return self._registrations.get(registration_id)

    def registrations(self) -> tuple[CapabilityRegistration, ...]:
        """All registrations in deterministic order."""

        return tuple(sorted(self._registrations.values(), key=_ordering_key))

    def by_name(self, name: str) -> tuple[CapabilityRegistration, ...]:
        return tuple(
            registration for registration in self.registrations() if registration.key.name == name
        )

    def by_key(self, key: CapabilityKey) -> tuple[CapabilityRegistration, ...]:
        return tuple(
            registration for registration in self.registrations() if registration.key == key
        )

    def __len__(self) -> int:
        return len(self._registrations)

    def to_dict(self) -> dict[str, Any]:
        return {"registrations": [reg.to_dict() for reg in self.registrations()]}

    # -------------------------------------------------------------- resolution

    def resolve(
        self,
        requirement: CapabilityRequirement,
        *,
        prefer: Iterable[str] | None = None,
    ) -> CapabilityResolution:
        """Resolve one requirement deterministically.

        ``prefer`` lists registration or provider identifiers that disambiguate a
        requirement satisfied by more than one provider. If it is absent and more
        than one provider satisfies the requirement, the result is ``ambiguous``.
        """

        same_name = self.by_name(requirement.name)
        candidates = tuple(
            registration for registration in same_name if requirement.accepts(registration)
        )
        if not candidates:
            status: ResolutionStatus = "no_provider" if not same_name else "version_mismatch"
            return CapabilityResolution(
                requirement=requirement,
                status=status,
                selected=None,
                candidates=(),
                same_name=same_name,
            )
        if len(candidates) == 1:
            return CapabilityResolution(
                requirement=requirement,
                status="resolved",
                selected=candidates[0],
                candidates=candidates,
                same_name=same_name,
            )

        preferred = _apply_preference(candidates, prefer)
        if preferred is not None:
            return CapabilityResolution(
                requirement=requirement,
                status="resolved",
                selected=preferred,
                candidates=candidates,
                same_name=same_name,
            )
        return CapabilityResolution(
            requirement=requirement,
            status="ambiguous",
            selected=None,
            candidates=candidates,
            same_name=same_name,
        )


class ScopedCapabilities:
    """Plugin-facing facade bound to one plugin instance scope.

    Every registration made through this facade is owned by the bound scope, so a
    plugin never writes matching withdrawal code.
    """

    __slots__ = ("_provider_id", "_provider_name", "_registry", "_scope")

    def __init__(
        self,
        registry: CapabilityRegistry,
        scope: Scope,
        provider_id: str,
        provider_name: str,
    ) -> None:
        self._registry = registry
        self._scope = scope
        self._provider_id = provider_id
        self._provider_name = provider_name

    def provide(
        self,
        key: CapabilityKey,
        value: object,
        *,
        version: str | Version | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> CapabilityRegistration:
        """Provide ``value`` under ``key`` for the lifetime of the plugin scope."""

        return self._registry.provide(
            scope=self._scope,
            key=key,
            value=value,
            version=version,
            provider_id=self._provider_id,
            provider_name=self._provider_name,
            metadata=metadata,
        )


def _registration_version(key: CapabilityKey, version: str | Version | None) -> Version:
    parsed = parse_version(version if version is not None else key.api_version, capability=key.name)
    if str(parsed.major) != key.api_version:
        raise ConfigurationError(
            "provided version does not match the capability contract generation",
            capability=str(key),
            version=str(parsed),
        )
    return parsed


def _apply_preference(
    candidates: tuple[CapabilityRegistration, ...], prefer: Iterable[str] | None
) -> CapabilityRegistration | None:
    if prefer is None:
        return None
    for identifier in prefer:
        for registration in candidates:
            if identifier in (registration.registration_id, registration.provider_id):
                return registration
    return None


def _ordering_key(registration: CapabilityRegistration) -> tuple[str, str, str]:
    return (
        registration.provider_name,
        registration.provider_id,
        registration.registration_id,
    )
