"""Instance installs must describe the configuration their plugin holds."""

from __future__ import annotations

import pytest
from tests.composition.support import tracked_provider

from chassis import ConfigurationError, Harness


def test_instance_install_accepts_an_equal_explicit_config() -> None:
    harness = Harness()
    plugin_type = tracked_provider("db", "database")
    instance = plugin_type({"pool": [1, 2]})

    harness.install(instance, entry_id="db", config={"pool": [1, 2]})

    entry = harness.entry("db")
    assert entry is not None
    assert dict(entry.config) == {"pool": (1, 2)}
    assert dict(entry.plugin.config) == {"pool": (1, 2)}


def test_instance_install_rejects_mismatched_config_without_mutation() -> None:
    harness = Harness()
    plugin_type = tracked_provider("db", "database")
    original = plugin_type({"pool": 1})
    harness.install(original, entry_id="db")
    previous_entry = harness.entry("db")
    assert previous_entry is not None

    with pytest.raises(ConfigurationError, match="must match its constructed config"):
        harness.install(
            plugin_type({"pool": 2}),
            entry_id="db",
            config={"pool": 3},
            replace=True,
        )

    assert harness.entry("db") is previous_entry
    assert previous_entry.revision == 1
    assert dict(previous_entry.config) == {"pool": 1}


def test_class_install_still_constructs_from_explicit_config() -> None:
    harness = Harness()
    plugin_type = tracked_provider("db", "database")

    harness.install(plugin_type, entry_id="db", config={"pool": 4})

    entry = harness.entry("db")
    assert entry is not None
    assert dict(entry.config) == {"pool": 4}
    assert dict(entry.plugin.config) == {"pool": 4}
