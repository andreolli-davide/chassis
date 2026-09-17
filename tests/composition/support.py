"""Shared builders for scoped-composition tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from chassis import Harness, PluginContext, plugin
from chassis.capabilities import CapabilityKey
from chassis.plugins.lifecycle import PluginInstance

if TYPE_CHECKING:
    from chassis.composition import ResolvedScope
    from chassis.core.generation import RuntimeGeneration


class TrackedProvider:
    """Provider payload that fails loudly if used after its scope closed."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.disposed = False

    def use(self) -> str:
        if self.disposed:
            raise RuntimeError(f"{self.name} used after dispose")
        return self.name


def tracked_provider(
    name: str,
    capability: str,
    *,
    version: str = "1.0.0",
    requires: Mapping[str, str] | None = None,
    disposals: list[str] | None = None,
):  # type: ignore[no-untyped-def]
    """A plugin providing one capability, recording disposal."""

    @plugin(
        name=name,
        version=version,
        provides={capability: version},
        requires=dict(requires or {}),
    )
    async def provider(ctx: PluginContext) -> None:
        value = TrackedProvider(name)
        ctx.capabilities.provide(
            CapabilityKey.from_version(capability, version), value, version=version
        )

        def on_close() -> None:
            value.disposed = True
            if disposals is not None:
                disposals.append(name)

        ctx.cleanup(f"{name} disposed", on_close)

    return provider


def consumer(
    name: str,
    *,
    requires: Mapping[str, str],
    provides: Mapping[str, str] | None = None,
    optional: Mapping[str, str] | None = None,
):  # type: ignore[no-untyped-def]
    """A plugin that resolves, but does not use, its requirements."""

    @plugin(
        name=name,
        version="1.0.0",
        provides=dict(provides or {}),
        requires=dict(requires),
        optional=dict(optional or {}),
    )
    async def plugin_consumer(ctx: PluginContext) -> None:
        for capability in requires:
            ctx.require(capability)
        for capability, version in (provides or {}).items():
            ctx.capabilities.provide(CapabilityKey.from_version(capability, version), name)

    return plugin_consumer


def failing(name: str):  # type: ignore[no-untyped-def]
    """A plugin whose setup raises after registering one capability."""

    @plugin(name=name, version="1.0.0", provides={"boom": "1.0.0"})
    async def failing_plugin(ctx: PluginContext) -> None:
        ctx.capabilities.provide(CapabilityKey("boom", "1"), name)
        raise RuntimeError(f"{name} failed during setup")

    return failing_plugin


def mounted(harness: Harness, entry_id: str) -> PluginInstance:
    """Return a mounted instance, failing when the entry is not mounted."""

    instance = harness.plugin_registry.instance(entry_id)
    assert instance is not None, f"entry {entry_id!r} is not mounted"
    return instance


def generation_of(harness: Harness) -> RuntimeGeneration:
    """Return the current generation, failing when none is published."""

    generation = harness.current_generation
    assert generation is not None, "no runtime generation is published"
    return generation


def scope_of(harness: Harness, path: str) -> ResolvedScope:
    """Return a scope of the current generation, failing when it is absent."""

    scope = generation_of(harness).scopes.get(path)
    assert scope is not None, f"scope {path!r} is not published"
    return scope
