"""Immutable capability views observed by agent runs (invariant I4).

A published :class:`CapabilitySnapshot` is never mutated. Runtime reconfiguration
builds a new snapshot in a new generation; an active run keeps observing the
snapshot it acquired for its whole lifetime.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from packaging.version import Version

from chassis.capabilities.keys import CapabilityKey, CapabilityRequirement
from chassis.capabilities.registry import (
    CapabilityRegistration,
    CapabilityResolution,
)
from chassis.core.errors import (
    CapabilityAmbiguous,
    CapabilityNotFound,
    CapabilityVersionMismatch,
)

__all__ = ["CapabilitySnapshot"]


def _registration_order(registration: CapabilityRegistration) -> tuple[str, str, str]:
    """Deterministic ordering for snapshot entries."""

    return (
        registration.provider_name,
        registration.provider_id,
        registration.registration_id,
    )


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """Immutable, deterministic view of the capabilities in one generation."""

    generation_id: str
    registrations: tuple[CapabilityRegistration, ...] = ()

    @classmethod
    def from_registrations(
        cls,
        generation_id: str,
        registrations: Iterable[CapabilityRegistration],
    ) -> CapabilitySnapshot:
        """Build a snapshot with entries in deterministic order."""

        ordered = tuple(sorted(registrations, key=_registration_order))
        return cls(generation_id=generation_id, registrations=ordered)

    def providers(self, capability: CapabilityKey | str) -> tuple[CapabilityRegistration, ...]:
        """All providers of a capability, in deterministic order."""

        name = capability.name if isinstance(capability, CapabilityKey) else capability
        matches = [item for item in self.registrations if item.key.name == name]
        if isinstance(capability, CapabilityKey) and capability.api_version:
            matches = [item for item in matches if item.key.api_version == capability.api_version]
        return tuple(matches)

    def get(self, capability: CapabilityKey | str) -> CapabilityRegistration | None:
        """The single provider of a capability.

        Raises:
            CapabilityAmbiguous: more than one provider matches.
        """

        matches = self.providers(capability)
        if len(matches) > 1:
            raise CapabilityAmbiguous(
                f"capability {capability} has multiple providers in this snapshot",
                capability=str(capability),
                generation_id=self.generation_id,
                providers=[item.provider_id for item in matches],
            )
        if len(matches) == 1:
            return matches[0]
        return None

    def require(self, capability: CapabilityKey | str) -> Any:
        """The provider object of a capability, or raise.

        Raises:
            CapabilityNotFound: no provider exists in this snapshot.
            CapabilityVersionMismatch: providers exist on a different contract
                generation than the requested key.
            CapabilityAmbiguous: more than one provider exists.
        """

        name = capability.name if isinstance(capability, CapabilityKey) else capability
        registration = self.get(capability)
        if registration is not None:
            return registration.value
        registered = tuple(item for item in self.registrations if item.key.name == name)
        if registered:
            offered = ", ".join(f"{item.provider_name}@{item.version}" for item in registered)
            raise CapabilityVersionMismatch(
                f"capability {name!r} is registered on a different contract generation",
                capability=name,
                generation_id=self.generation_id,
                registered=offered,
                requested=str(capability),
            )
        raise CapabilityNotFound(
            f"capability {name!r} is not registered in this snapshot",
            capability=name,
            generation_id=self.generation_id,
        )

    def resolve(
        self,
        requirement: CapabilityRequirement,
        *,
        prefer: Iterable[str] | None = None,
    ) -> CapabilityResolution:
        """Resolve a requirement against this snapshot, deterministically."""

        # Filter by name only here: a provider on another contract generation is a
        # *version mismatch* for diagnostics, not an absent capability.
        same_name = self.providers(requirement.name)
        candidates = tuple(
            item for item in same_name if requirement.accepts(item.key, item.version)
        )
        if not candidates:
            return CapabilityResolution(
                requirement=requirement,
                status="no_provider" if not same_name else "version_mismatch",
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
        for identifier in prefer or ():
            for item in candidates:
                if identifier in (item.registration_id, item.provider_id):
                    return CapabilityResolution(
                        requirement=requirement,
                        status="resolved",
                        selected=item,
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

    def versions(self) -> dict[str, tuple[str, ...]]:
        """Capability name to the versions provided, for snapshot metadata."""

        collected: dict[str, list[str]] = {}
        for registration in self.registrations:
            collected.setdefault(registration.key.name, []).append(str(registration.version))
        return {
            name: tuple(sorted(versions, key=Version))
            for name, versions in sorted(collected.items())
        }

    def __len__(self) -> int:
        return len(self.registrations)

    def __contains__(self, capability: object) -> bool:
        return (
            bool(self.providers(capability))
            if isinstance(capability, (str, CapabilityKey))
            else False
        )

    def to_dict(self) -> dict[str, Any]:
        """Diagnostic view: registrations, never provider payloads."""

        return {
            "generation_id": self.generation_id,
            "registrations": [item.to_dict() for item in self.registrations],
        }
