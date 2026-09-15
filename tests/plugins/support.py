"""Shared builders for resolver-level tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from packaging.version import Version

from chassis.capabilities.keys import CapabilityKey
from chassis.capabilities.registry import CapabilityRegistration
from chassis.plugins.manifest import PluginManifest
from chassis.plugins.resolver import PluginCandidate


def candidate(
    entry_id: str,
    *,
    provides: Mapping[str, str] | None = None,
    requires: Mapping[str, str] | None = None,
    optional: Mapping[str, str] | None = None,
    active: bool = False,
    instance_id: str | None = None,
    registrations: Sequence[CapabilityRegistration] = (),
    version: str = "1.0.0",
) -> PluginCandidate:
    """Build a resolver candidate with a minimal manifest."""

    manifest = PluginManifest(
        name=entry_id,
        version=version,
        provides=dict(provides or {}),
        requires=dict(requires or {}),
        optional=dict(optional or {}),
    )
    return PluginCandidate(
        entry_id=entry_id,
        manifest=manifest,
        instance_id=instance_id,
        active=active,
        registrations=tuple(registrations),
    )


def registration(
    provider_id: str,
    provider_name: str,
    capability: str,
    version: str,
) -> CapabilityRegistration:
    """Build a live capability registration for an active candidate."""

    return CapabilityRegistration(
        registration_id=f"cap_{provider_id}_{capability}",
        key=CapabilityKey.from_version(capability, version),
        version=Version(version),
        value=object(),
        provider_id=provider_id,
        provider_name=provider_name,
        scope_id="scope_test",
    )
