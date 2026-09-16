from __future__ import annotations

from pathlib import Path

import pytest

from chassis import MODEL, PluginContext, plugin
from chassis.config import (
    DesiredStateAction,
    HarnessConfig,
    InstalledEntry,
    PluginCatalog,
    PluginEntryConfig,
    config_fingerprint,
    diff_desired_state,
    load_config,
    parse_config,
)
from chassis.core.errors import ConfigurationError
from chassis.testing import TestHarness

YAML = """
version: 1
plugins:
  - id: primary-model
    plugin: fake-model
    config:
      model: example-model
  - id: search
    plugin: web-search
provider_preferences:
  database: postgres
"""


@plugin(name="fake-model", version="1.0.0", provides={"model": "1.0.0"})
async def fake_model(ctx: PluginContext) -> None:
    ctx.capabilities.provide(MODEL, ctx.config.get("model", "default-model"))


@plugin(name="web-search", version="1.0.0", provides={"tools": "1.0.0"})
async def web_search(ctx: PluginContext) -> None:
    ctx.capabilities.provide(
        __import__("chassis.capabilities", fromlist=["TOOLS"]).TOOLS, "search-tools"
    )


def test_yaml_configuration_parses_into_models() -> None:
    config = parse_config(YAML)

    assert config.version == 1
    assert [entry.id for entry in config.plugins] == ["primary-model", "search"]
    assert config.entry("primary-model").config == {"model": "example-model"}  # type: ignore[union-attr]
    assert config.provider_preferences == {"database": "postgres"}
    assert config.to_dict()["plugins"][0]["config_keys"] == ["model"]


def test_json_and_mapping_sources_parse() -> None:
    from_mapping = parse_config({"plugins": [{"id": "a", "plugin": "x"}]})
    from_json = parse_config('{"plugins": [{"id": "a", "plugin": "x"}]}')

    assert from_mapping == from_json


def test_configuration_loads_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "harness.yaml"
    path.write_text(YAML, encoding="utf-8")

    config = load_config(path)

    assert [entry.id for entry in config.plugins] == ["primary-model", "search"]


def test_invalid_configuration_is_rejected_with_a_typed_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        parse_config({"plugins": [{"id": "", "plugin": "x"}]})
    with pytest.raises(ConfigurationError):
        parse_config({"plugins": [{"id": "a", "plugin": "x", "unknown": 1}]})
    with pytest.raises(ConfigurationError):
        parse_config({"plugins": [{"id": "a", "plugin": "x"}, {"id": "a", "plugin": "y"}]})
    with pytest.raises(ConfigurationError):
        load_config(tmp_path / "missing.yaml")
    with pytest.raises(ConfigurationError):
        parse_config(42)


def test_disabled_entries_are_not_desired() -> None:
    config = parse_config(
        {"plugins": [{"id": "a", "plugin": "x", "enabled": False}, {"id": "b", "plugin": "y"}]}
    )

    assert [entry.id for entry in config.enabled_entries] == ["b"]


def test_catalog_resolves_implementation_names() -> None:
    catalog = PluginCatalog()
    catalog.register("web-search", web_search)

    assert catalog.get("web-search") is web_search
    assert "web-search" in catalog
    assert catalog.names() == ("web-search",)

    with pytest.raises(ConfigurationError):
        catalog.register("web-search", web_search)
    with pytest.raises(ConfigurationError) as excinfo:
        catalog.get("missing")

    assert excinfo.value.context["available"] == ["web-search"]


def installed(
    entry_id: str, plugin_name: str, config: dict[str, object] | None = None
) -> InstalledEntry:
    return InstalledEntry(
        entry_id=entry_id,
        plugin=plugin_name,
        revision=1,
        config_fingerprint=config_fingerprint(plugin=plugin_name, config=config or {}),
    )


def test_diff_derives_add_remove_and_unchanged() -> None:
    desired = parse_config(
        {"plugins": [{"id": "keep", "plugin": "x"}, {"id": "new", "plugin": "y"}]}
    )
    current = {"keep": installed("keep", "x"), "gone": installed("gone", "z")}

    changes = diff_desired_state(desired, current)
    by_id = {change.entry_id: change for change in changes}

    assert by_id["keep"].action is DesiredStateAction.UNCHANGED
    assert by_id["new"].action is DesiredStateAction.ADD
    assert by_id["gone"].action is DesiredStateAction.REMOVE
    assert [change.entry_id for change in changes] == ["gone", "keep", "new"]


def test_diff_derives_replace_for_implementation_or_config_change() -> None:
    desired = parse_config(
        {
            "plugins": [
                {"id": "swapped", "plugin": "y"},
                {"id": "tuned", "plugin": "x", "config": {"retries": 5}},
            ]
        }
    )
    current = {
        "swapped": installed("swapped", "x"),
        "tuned": installed("tuned", "x", {"retries": 1}),
    }

    changes = {change.entry_id: change for change in diff_desired_state(desired, current)}

    assert changes["swapped"].action is DesiredStateAction.REPLACE
    assert "implementation changed" in changes["swapped"].reason
    assert changes["tuned"].action is DesiredStateAction.REPLACE
    assert "reconfigure is not supported" in changes["tuned"].reason


def test_diff_never_emits_reconfigure() -> None:
    desired = parse_config({"plugins": [{"id": "a", "plugin": "x", "config": {"v": 2}}]})
    current = {"a": installed("a", "x", {"v": 1})}

    changes = diff_desired_state(desired, current)

    assert changes[0].action is not DesiredStateAction.RECONFIGURE


def test_entry_configuration_is_immutable() -> None:
    entry = PluginEntryConfig(id="a", plugin="x", config={"k": 1})

    with pytest.raises(TypeError):
        entry.config["k"] = 2  # type: ignore[index]  # type: ignore[index]
    with pytest.raises(TypeError):
        entry.config.update({"k": 2})
    assert entry.config.copy() == {"k": 1}


async def test_applying_configuration_mounts_plugins_from_the_catalog() -> None:
    async with TestHarness() as harness:
        harness.register_plugin_type("fake-model", fake_model)
        harness.register_plugin_type("web-search", web_search)

        result = harness.apply_config(YAML)

        assert [change.action for change in result.applied] == [
            DesiredStateAction.ADD,
            DesiredStateAction.ADD,
        ]

        await harness.reconcile()
        assert {entry.entry_id for entry in harness.plugin_registry.entries()} == {
            "primary-model",
            "search",
        }
        assert harness.plugin_registry.instance("primary-model") is not None

        async with harness.acquire() as generation:
            assert generation.snapshot.require(MODEL) == "example-model"
        assert harness.config is not None


async def test_reapplying_an_unchanged_configuration_is_a_no_op() -> None:
    async with TestHarness() as harness:
        harness.register_plugin_type("fake-model", fake_model)
        harness.apply_config({"plugins": [{"id": "model", "plugin": "fake-model"}]})
        await harness.reconcile()
        generation = harness.current_generation

        result = harness.apply_config({"plugins": [{"id": "model", "plugin": "fake-model"}]})
        await harness.reconcile()

        assert [change.action for change in result.changes] == [DesiredStateAction.UNCHANGED]
        assert result.applied == ()
        assert harness.current_generation is generation


async def test_changing_plugin_configuration_replaces_the_instance() -> None:
    async with TestHarness() as harness:
        harness.register_plugin_type("fake-model", fake_model)
        harness.apply_config(
            {"plugins": [{"id": "model", "plugin": "fake-model", "config": {"model": "first"}}]}
        )
        await harness.reconcile()
        first_instance = harness.instance("model")

        harness.apply_config(
            {"plugins": [{"id": "model", "plugin": "fake-model", "config": {"model": "second"}}]}
        )
        await harness.reconcile()

        assert harness.instance("model").instance_id != first_instance.instance_id
        async with harness.acquire() as generation:
            assert generation.snapshot.require(MODEL) == "second"


async def test_removing_an_entry_from_configuration_unloads_it() -> None:
    async with TestHarness() as harness:
        harness.register_plugin_type("fake-model", fake_model)
        harness.apply_config({"plugins": [{"id": "model", "plugin": "fake-model"}]})
        await harness.reconcile()
        assert harness.instance("model").state.value == "active"

        result = harness.apply_config({"plugins": []})
        await harness.reconcile()

        assert [change.action for change in result.applied] == [DesiredStateAction.REMOVE]
        assert harness.plugin_registry.entries() == ()
        assert harness.plugin_registry.instances() == ()


async def test_disabling_an_entry_removes_it_without_deleting_configuration() -> None:
    async with TestHarness() as harness:
        harness.register_plugin_type("fake-model", fake_model)
        harness.apply_config({"plugins": [{"id": "model", "plugin": "fake-model"}]})
        await harness.reconcile()

        harness.apply_config(
            {"plugins": [{"id": "model", "plugin": "fake-model", "enabled": False}]}
        )
        await harness.reconcile()

        assert harness.plugin_registry.entries() == ()


async def test_provider_preferences_from_configuration_are_applied() -> None:
    @plugin(name="db-a", version="1.0.0", provides={"database": "1.0.0"})
    async def db_a(ctx: PluginContext) -> None:
        ctx.capabilities.provide(
            __import__("chassis.capabilities", fromlist=["DATABASE"]).DATABASE, "a"
        )

    @plugin(name="db-b", version="1.0.0", provides={"database": "1.0.0"})
    async def db_b(ctx: PluginContext) -> None:
        ctx.capabilities.provide(
            __import__("chassis.capabilities", fromlist=["DATABASE"]).DATABASE, "b"
        )

    @plugin(name="consumer", version="1.0.0", requires={"database": ">=1,<2"})
    async def consumer(ctx: PluginContext) -> None:
        ctx.require("database")

    async with TestHarness() as harness:
        harness.register_plugin_type("db-a", db_a)
        harness.register_plugin_type("db-b", db_b)
        harness.register_plugin_type("consumer", consumer)

        harness.apply_config(
            {
                "plugins": [
                    {"id": "db-a", "plugin": "db-a"},
                    {"id": "db-b", "plugin": "db-b"},
                    {"id": "consumer", "plugin": "consumer"},
                ],
                "provider_preferences": {"consumer:database": "db-b"},
            }
        )
        await harness.reconcile()

        assert harness.instance("consumer").resolved["database"].provider_id == (
            harness.instance("db-b").instance_id
        )


async def test_unknown_implementation_is_reported() -> None:
    async with TestHarness() as harness:
        with pytest.raises(ConfigurationError) as excinfo:
            harness.apply_config({"plugins": [{"id": "x", "plugin": "missing"}]})

        assert excinfo.value.context["plugin"] == "missing"


async def test_configuration_diagnostics_are_available() -> None:
    async with TestHarness() as harness:
        harness.register_plugin_type("fake-model", fake_model)
        result = harness.apply_config({"plugins": [{"id": "model", "plugin": "fake-model"}]})

        payload = result.to_dict()
        assert payload["changes"][0]["action"] == "add"
        assert HarnessConfig.model_validate(result.config.model_dump()) == result.config


async def test_configuration_diagnostics_explain_pending_changes() -> None:
    async with TestHarness() as harness:
        harness.register_plugin_type("fake-model", fake_model)
        harness.apply_config({"plugins": [{"id": "model", "plugin": "fake-model"}]})
        await harness.reconcile()

        assert harness.diagnostics.config() is not None
        assert harness.diagnostics.desired_state() == [
            {
                "action": "unchanged",
                "entry_id": "model",
                "plugin": "fake-model",
                "reason": "",
            }
        ]

        # An entry installed outside the configuration shows up as a divergence.
        harness.install(fake_model, entry_id="extra")
        assert [item["action"] for item in harness.diagnostics.desired_state()] == ["remove"]

        # Applying the configuration converges, so drift disappears.
        harness.apply_config({"plugins": [{"id": "model", "plugin": "fake-model"}]})
        assert [item["action"] for item in harness.diagnostics.desired_state()] == [
            "unchanged"
        ]


async def test_diagnostics_report_no_desired_state_without_configuration() -> None:
    async with TestHarness() as harness:
        assert harness.diagnostics.config() is None
        assert harness.diagnostics.desired_state() == []
