"""Declarative configuration models.

Desired state has *stable plugin entry identities*. Reconciliation compares
identity, not list position, which is what makes ADD/REMOVE/REPLACE meaningful and
what keeps a reordered configuration file from looking like a rewrite. YAML and
JSON ordering therefore never defines dependency semantics -- capabilities do.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chassis.core.collections import freeze

__all__ = ["HarnessConfig", "PluginEntryConfig"]


class PluginEntryConfig(BaseModel):
    """One desired plugin entry.

    Args:
        id: Stable entry identity. Must not change when the plugin or its
            configuration changes, or reconciliation sees a remove plus an add
            instead of a replace.
        plugin: Plugin implementation name, resolved through the catalog.
        config: Plugin configuration passed to the implementation.
        enabled: Whether the entry is desired. A disabled entry is treated as
            absent so a config file can park a plugin without deleting it.
        provider_preference: Capability name to provider entry id, used to
            disambiguate requirements this entry has.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    plugin: str = Field(min_length=1)
    config: Mapping[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    provider_preference: Mapping[str, str] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("plugin entry id must not have surrounding whitespace")
        return value

    @model_validator(mode="after")
    def _freeze_configuration(self) -> PluginEntryConfig:
        # A frozen model blocks attribute assignment but not mutation of the
        # containers it holds; desired state must be immutable in both senses,
        # so every stored container is deep-frozen and copied: no nested
        # structure stays mutable or aliases the caller's input.
        declared = self.config.get("config_version")
        if declared is not None and (
            not isinstance(declared, int) or isinstance(declared, bool) or declared < 0
        ):
            raise ValueError("config_version must be a non-negative integer")
        object.__setattr__(self, "config", freeze(self.config))
        object.__setattr__(self, "provider_preference", freeze(self.provider_preference))
        return self

    @property
    def config_version(self) -> int | None:
        """Declared configuration version, when the entry pins one."""

        value = self.config.get("config_version")
        return value if isinstance(value, int) else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plugin": self.plugin,
            "config_keys": sorted(self.config),
            "enabled": self.enabled,
            "provider_preference": dict(sorted(self.provider_preference.items())),
        }


class HarnessConfig(BaseModel):
    """Complete desired state for one harness."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    version: int = 1
    plugins: tuple[PluginEntryConfig, ...] = ()
    provider_preferences: Mapping[str, str] = Field(default_factory=dict)

    @field_validator("version", mode="before")
    @classmethod
    def _reject_boolean_schema_version(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("configuration schema version must be an integer")
        return value

    @field_validator("version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError(f"unsupported configuration schema version: {value}")
        return value

    @model_validator(mode="after")
    def _freeze_preferences(self) -> HarnessConfig:
        object.__setattr__(self, "provider_preferences", freeze(self.provider_preferences))
        return self

    @field_validator("plugins")
    @classmethod
    def _unique_ids(cls, value: tuple[PluginEntryConfig, ...]) -> tuple[PluginEntryConfig, ...]:
        seen: set[str] = set()
        for entry in value:
            if entry.id in seen:
                raise ValueError(f"duplicate plugin entry id: {entry.id!r}")
            seen.add(entry.id)
        return value

    @property
    def enabled_entries(self) -> tuple[PluginEntryConfig, ...]:
        return tuple(entry for entry in self.plugins if entry.enabled)

    def entry(self, entry_id: str) -> PluginEntryConfig | None:
        for entry in self.plugins:
            if entry.id == entry_id:
                return entry
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "plugins": [entry.to_dict() for entry in self.plugins],
            "provider_preferences": dict(sorted(self.provider_preferences.items())),
        }
