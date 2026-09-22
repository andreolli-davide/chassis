"""Loading declarative configuration and resolving plugin implementations.

Configuration names a plugin implementation; the catalog maps that name to the
class that implements it. Keeping the mapping explicit avoids import-time plugin
registration, which would make composition depend on which modules happened to be
imported.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from chassis.config.models import HarnessConfig
from chassis.core.errors import ConfigurationError
from chassis.plugins.base import Plugin

__all__ = ["PluginCatalog", "load_config", "parse_config"]

ConfigSource = "HarnessConfig | Mapping[str, Any] | str | Path"


class PluginCatalog:
    """Maps implementation names to plugin classes."""

    def __init__(self) -> None:
        self._plugins: dict[str, type[Plugin]] = {}

    def register(self, name: str, plugin_type: type[Plugin], *, replace: bool = False) -> None:
        """Register an implementation under ``name``."""

        if not name:
            raise ConfigurationError("plugin catalog name must not be empty")
        if not isinstance(plugin_type, type):
            raise ConfigurationError(
                "catalog entries must be plugin classes",
                plugin=name,
                value=type(plugin_type).__name__,
            )
        if name in self._plugins and not replace:
            raise ConfigurationError("plugin implementation is already registered", plugin=name)
        self._plugins[name] = plugin_type

    def unregister(self, name: str) -> bool:
        return self._plugins.pop(name, None) is not None

    def get(self, name: str) -> type[Plugin]:
        """Return a registered implementation.

        Raises:
            ConfigurationError: the name is unknown.
        """

        plugin_type = self._plugins.get(name)
        if plugin_type is None:
            raise ConfigurationError(
                "plugin implementation is not registered",
                plugin=name,
                available=sorted(self._plugins),
            )
        return plugin_type

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._plugins))

    def __len__(self) -> int:
        return len(self._plugins)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._plugins

    def to_dict(self) -> dict[str, Any]:
        return {"plugins": list(self.names())}


_CONFIG_MIGRATIONS: dict[int, Callable[[Mapping[str, Any]], Mapping[str, Any]]] = {
    1: lambda payload: payload,
}


def migrate_config(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Migrate a raw configuration payload to the supported schema version.

    An explicit dispatcher: a schema version this build does not know is
    rejected here, before validation, so accepting a second version is a
    deliberate, tested change rather than a silent guess.
    """

    version = payload.get("version", 1)
    migration = _CONFIG_MIGRATIONS.get(version) if isinstance(version, int) else None
    if migration is None:
        raise ConfigurationError(
            "unsupported configuration schema version",
            version=version,
            supported=sorted(_CONFIG_MIGRATIONS),
        )
    return migration(payload)


def parse_config(source: Any) -> HarnessConfig:
    """Parse configuration from a mapping, a YAML/JSON string, or a path.

    Raises:
        ConfigurationError: the document cannot be read or does not validate.
    """

    if isinstance(source, HarnessConfig):
        return source
    if isinstance(source, Path):
        return _parse_path(source)
    if isinstance(source, Mapping):
        return _validate(source, origin="mapping")
    if isinstance(source, str):
        stripped = source.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return _validate(json.loads(source), origin="json string")
            except json.JSONDecodeError as error:
                raise ConfigurationError(
                    "configuration is not valid JSON", error=str(error)
                ) from error
        if "\n" in stripped or ":" in stripped:
            return _validate(_load_yaml(stripped, origin="yaml string"), origin="yaml string")
        candidate = Path(source)
        if candidate.exists():
            return _parse_path(candidate)
        raise ConfigurationError("configuration string is neither YAML nor an existing path")
    raise ConfigurationError("unsupported configuration source", value_type=type(source).__name__)


def load_config(path: str | Path) -> HarnessConfig:
    """Load configuration from a file path."""

    return _parse_path(Path(path))


def _parse_path(path: Path) -> HarnessConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigurationError("configuration file cannot be read", path=str(path)) from error
    if path.suffix.lower() == ".json":
        try:
            return _validate(json.loads(text), origin=str(path))
        except json.JSONDecodeError as error:
            raise ConfigurationError("configuration is not valid JSON", path=str(path)) from error
    return _validate(_load_yaml(text, origin=str(path)), origin=str(path))


def _load_yaml(text: str, *, origin: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ConfigurationError("configuration is not valid YAML", path=origin) from error


def _validate(payload: Any, *, origin: str) -> HarnessConfig:
    if payload is None:
        return HarnessConfig()
    if not isinstance(payload, Mapping):
        raise ConfigurationError(
            "configuration must be a mapping", path=origin, value_type=type(payload).__name__
        )
    payload = migrate_config(payload)
    try:
        return HarnessConfig.model_validate(dict(payload))
    except ValidationError as error:
        raise ConfigurationError(
            "configuration is invalid", path=origin, errors=error.error_count()
        ) from error
