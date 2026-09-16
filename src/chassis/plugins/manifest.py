"""Plugin manifests: what a plugin provides, requires, and is allowed to do.

Manifest version requirements use :mod:`packaging` (invariant I12 relies on
manifests being parsed deterministically and identically everywhere).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chassis.capabilities.keys import (
    CapabilityKey,
    CapabilityRequirement,
    parse_version,
)
from chassis.core.collections import FrozenDict
from chassis.core.errors import ConfigurationError

__all__ = ["PluginManifest"]


class PluginManifest(BaseModel):
    """Declarative description of a plugin implementation.

    Args:
        name: Plugin implementation name, stable across versions.
        version: Implementation version (PEP 440).
        provides: Capability name to provided implementation version.
        requires: Capability name to required version specifier.
        optional: Capability name to optional version specifier. An unsatisfied
            optional requirement does not prevent activation.
        permissions: Permissions the plugin declares it needs at harness
            boundaries. Declaring a permission is not a sandbox; in-process
            plugins are trusted code.
        config_version: Schema version of the plugin's configuration.
        metadata: Free-form, non-secret metadata.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    version: str
    provides: Mapping[str, str] = Field(default_factory=dict)
    requires: Mapping[str, str] = Field(default_factory=dict)
    optional: Mapping[str, str] = Field(default_factory=dict)
    permissions: Sequence[str] = ()
    config_version: int = 1
    metadata: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        try:
            parse_version(value, capability=cls.__name__)
        except ConfigurationError as error:
            raise ValueError(str(error)) from error
        return value

    @field_validator("permissions")
    @classmethod
    def _freeze_permissions(cls, value: Sequence[str]) -> Sequence[str]:
        return tuple(value)

    @model_validator(mode="after")
    def _validate_capabilities(self) -> PluginManifest:
        """Reject unparseable capability declarations when the manifest is built."""

        try:
            for name, provided in self.provides.items():
                CapabilityKey.from_version(name, provided)
            for name, requirement in {**self.requires, **self.optional}.items():
                CapabilityRequirement.parse(name, requirement)
        except ConfigurationError as error:
            raise ValueError(str(error)) from error
        for field in ("provides", "requires", "optional", "metadata"):
            object.__setattr__(self, field, FrozenDict(getattr(self, field)))
        return self

    @property
    def parsed_version(self) -> Version:
        return parse_version(self.version, capability=self.name)

    @property
    def identity(self) -> str:
        """Implementation identity, stable for a given name and version."""

        return f"{self.name}@{self.version}"

    def provided_keys(self) -> tuple[CapabilityKey, ...]:
        """Capability contracts this plugin implements."""

        return tuple(
            CapabilityKey.from_version(name, version)
            for name, version in sorted(self.provides.items())
        )

    def required_capabilities(self) -> tuple[CapabilityRequirement, ...]:
        """Hard requirements, in deterministic order."""

        return tuple(
            CapabilityRequirement.parse(name, requirement)
            for name, requirement in sorted(self.requires.items())
        )

    def optional_capabilities(self) -> tuple[CapabilityRequirement, ...]:
        """Soft requirements, in deterministic order."""

        return tuple(
            CapabilityRequirement.parse(name, requirement, optional=True)
            for name, requirement in sorted(self.optional.items())
        )

    def to_dict(self) -> dict[str, Any]:
        """Serializable, non-secret description used by diagnostics and snapshots."""

        return {
            "name": self.name,
            "version": self.version,
            "provides": dict(sorted(self.provides.items())),
            "requires": dict(sorted(self.requires.items())),
            "optional": dict(sorted(self.optional.items())),
            "permissions": sorted(self.permissions),
            "config_version": self.config_version,
            "metadata": dict(sorted(self.metadata.items())),
        }
