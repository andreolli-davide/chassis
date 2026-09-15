"""Versioned capabilities: contracts, requirements, and provider registries."""

from __future__ import annotations

from chassis.capabilities.keys import (
    ARTIFACTS,
    DATABASE,
    MEMORY,
    MODEL,
    POLICY,
    SANDBOX,
    SCHEDULER,
    SECRETS,
    TOOLS,
    CapabilityKey,
    CapabilityRequirement,
    parse_specifier,
    parse_version,
    specifier_major,
)
from chassis.capabilities.registry import (
    CapabilityRegistration,
    CapabilityRegistry,
    CapabilityResolution,
    ResolutionStatus,
    ScopedCapabilities,
)
from chassis.capabilities.snapshot import CapabilitySnapshot

__all__ = [
    "ARTIFACTS",
    "DATABASE",
    "MEMORY",
    "MODEL",
    "POLICY",
    "SANDBOX",
    "SCHEDULER",
    "SECRETS",
    "TOOLS",
    "CapabilityKey",
    "CapabilityRegistration",
    "CapabilityRegistry",
    "CapabilityRequirement",
    "CapabilityResolution",
    "CapabilitySnapshot",
    "ResolutionStatus",
    "ScopedCapabilities",
    "parse_specifier",
    "parse_version",
    "specifier_major",
]
