"""Composition-tree ownership and naming are validated (roadmap R012).

A parent scope must belong to the same tree, scope paths must be canonical and
absolute everywhere, and whitespace-only or untrimmed names are rejected at the
earliest boundary.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from chassis import Harness, PluginContext, plugin
from chassis.agent_spec import AgentSpec
from chassis.agents import AgentRegistry
from chassis.capabilities import CapabilityKey
from chassis.composition import CompositionScope
from chassis.config.loader import PluginCatalog
from chassis.core.errors import ConfigurationError
from chassis.plugins.manifest import PluginManifest
from chassis.policy.permissions import Permission


def dummy():  # type: ignore[no-untyped-def]
    @plugin(name="dummy", version="1.0.0")
    async def provide(ctx: PluginContext) -> None:
        return None

    return provide


def test_a_parent_scope_from_another_tree_is_rejected() -> None:
    tree_a = Harness(name="a").composition
    tree_b = Harness(name="b").composition
    foreign = tree_a.child("research")

    with pytest.raises(ConfigurationError) as excinfo:
        CompositionScope(tree_b, "mine", parent=foreign)

    assert excinfo.value.context["parent"] == "/research"
    with pytest.raises(ConfigurationError):
        tree_b.attach(CompositionScope(tree_b, "ok", parent=foreign))


BAD_PATHS = (
    "research",
    "/research/",
    "//research",
    "/a//b",
    "/./x",
    "/a/..",
    " /x",
    "/x ",
    "/a/ /b",
    "",
)


def test_scope_paths_must_be_canonical_and_absolute() -> None:
    harness = Harness()
    harness.install(dummy(), entry_id="db")

    for bad in BAD_PATHS:
        with pytest.raises(ConfigurationError):
            harness.composition.get(bad)
        with pytest.raises(ConfigurationError):
            harness.composition.ensure(bad)
        with pytest.raises(ConfigurationError):
            harness.composition.remove(bad)
        with pytest.raises(ConfigurationError):
            AgentSpec(name="probe", revision="1", scope=bad)

    assert harness.composition.ensure("/research/deep") is not None


def test_whitespace_only_names_are_rejected() -> None:
    for bad in ("", " ", " x", "x "):
        with pytest.raises(ConfigurationError):
            CapabilityKey(bad, "1")
        with pytest.raises(ConfigurationError):
            Permission(bad)
        with pytest.raises(ConfigurationError):
            PluginCatalog().register(bad, dummy())  # type: ignore[arg-type]

    harness = Harness()
    for bad in ("", " ", " x"):
        with pytest.raises(ConfigurationError):
            harness.install(dummy(), entry_id=bad)

    class NamedRuntime:
        @property
        def name(self) -> str:
            return " bad "

        async def invoke(self, request: object, run_context: object) -> object:
            return None

        async def stream(self, request: object, run_context: object) -> object:
            return None

    with pytest.raises(ConfigurationError):
        AgentRegistry().register(NamedRuntime())  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError):
        AgentSpec(name=" ", revision="1")


def test_schema_versions_must_be_non_negative() -> None:
    with pytest.raises(ValidationError):
        PluginManifest(name="x", version="1.0.0", config_version=-1)

    harness = Harness()
    with pytest.raises(ConfigurationError):
        harness.apply_config(
            {
                "version": 1,
                "plugins": [{"id": "db", "plugin": "dummy", "config": {"config_version": -1}}],
            }
        )
