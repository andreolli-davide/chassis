"""Declarative configuration must be a true deep freeze, not a shallow wrap.

Desired state is observed by runs and compared across revisions, so it must be
immutable in every sense: mutating the caller's input after construction must
not change the config, and no nested container reachable from the models may
accept mutation. A ``FrozenDict`` around the outer mapping alone leaves both
holes open.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import ValidationError

from chassis.config import HarnessConfig, PluginEntryConfig
from chassis.core.collections import FrozenDict


def test_mutating_the_input_after_construction_cannot_change_the_config() -> None:
    nested: dict[str, Any] = {
        "database": {"dsn": "postgres://local", "pool": {"size": 2}},
        "retries": [1, 2],
    }
    preference: dict[str, str] = {"database": "primary"}
    preferences: dict[str, str] = {"database": "primary"}
    config = HarnessConfig(
        plugins=(
            PluginEntryConfig(
                id="db",
                plugin="postgres",
                config=nested,
                provider_preference=preference,
            ),
        ),
        provider_preferences=preferences,
    )
    entry = config.plugins[0]

    # The author keeps mutating the input after handing it over.
    nested["database"]["dsn"] = "postgres://mutated"
    nested["database"]["pool"]["size"] = 99
    nested["retries"].append(3)
    nested["late"] = "added"
    preference["database"] = "backup"
    preferences["database"] = "backup"

    assert entry.config["database"] == {"dsn": "postgres://local", "pool": {"size": 2}}
    assert list(entry.config["retries"]) == [1, 2]
    assert "late" not in entry.config
    assert entry.provider_preference == {"database": "primary"}
    assert config.provider_preferences == {"database": "primary"}

    # The config shares no container with the caller's input at any depth.
    assert entry.config is not nested
    assert entry.config["database"] is not nested["database"]
    assert entry.config["retries"] is not nested["retries"]


def test_no_mutable_structure_is_reachable_through_the_config_surface() -> None:
    nested: dict[str, Any] = {
        "database": {"dsn": "postgres://local", "pool": {"size": 2}},
        "retries": [1, 2],
    }
    config = HarnessConfig(
        plugins=(
            PluginEntryConfig(
                id="db",
                plugin="postgres",
                config=nested,
                provider_preference={"database": "primary"},
            ),
        ),
        provider_preferences={"database": "primary"},
    )
    entry = config.plugins[0]

    # Attribute assignment on the frozen models.
    with pytest.raises(ValidationError):
        entry.config = nested  # type: ignore[misc]
    with pytest.raises(ValidationError):
        config.provider_preferences = {}  # type: ignore[misc]

    # Mapping access on every stored mapping.
    with pytest.raises(TypeError):
        entry.config["late"] = 1  # type: ignore[index]
    surface = cast("FrozenDict", entry.config)
    with pytest.raises(TypeError):
        surface.update({"late": 1})
    with pytest.raises(TypeError):
        surface.pop("database")
    with pytest.raises(TypeError):
        surface.setdefault("late", 1)
    with pytest.raises(TypeError):
        surface.clear()
    with pytest.raises(TypeError):
        surface.popitem()
    with pytest.raises(TypeError):
        entry.provider_preference["database"] = "backup"  # type: ignore[index]
    with pytest.raises(TypeError):
        config.provider_preferences["database"] = "backup"  # type: ignore[index]

    # Nested values: this is where a shallow freeze leaks.
    with pytest.raises(TypeError):
        entry.config["database"]["dsn"] = "postgres://mutated"  # type: ignore[index]
    with pytest.raises(TypeError):
        del entry.config["database"]["dsn"]  # type: ignore[index]
    with pytest.raises(TypeError):
        entry.config["database"].update({"dsn": "postgres://mutated"})
    with pytest.raises(TypeError):
        entry.config["database"]["pool"]["size"] = 99  # type: ignore[index]
    with pytest.raises(TypeError):
        entry.config["retries"][0] = 9  # type: ignore[index]
    with pytest.raises(TypeError):
        del entry.config["retries"][0]  # type: ignore[index]

    # Nothing got through: the config still observes exactly the input values.
    assert entry.config["database"] == {"dsn": "postgres://local", "pool": {"size": 2}}
    assert list(entry.config["retries"]) == [1, 2]
    assert set(entry.config) == {"database", "retries"}
    assert entry.provider_preference == {"database": "primary"}
    assert config.provider_preferences == {"database": "primary"}
