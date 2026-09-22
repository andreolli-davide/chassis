"""Shared plugin fakes for the planning-contract tests."""

from __future__ import annotations

from chassis import DATABASE, MEMORY, PluginContext, plugin


@plugin(name="orders-db", version="1.0.0", provides={"database": "1.0.0"})
async def db_provider(ctx: PluginContext) -> None:
    ctx.capabilities.provide(DATABASE, f"{ctx.entry_id}-handle")


@plugin(
    name="memory", version="2.1.0", provides={"memory": "1.0.0"}, requires={"database": ">=1,<2"}
)
async def memory_consumer(ctx: PluginContext) -> None:
    ctx.require("database")
    ctx.capabilities.provide(MEMORY, "memory-handle")


@plugin(name="notes", version="1.0.0", provides={"memory": "1.0.0"}, requires={"memory": ">=1"})
async def notes_a(ctx: PluginContext) -> None:
    ctx.require("memory")


@plugin(name="recall", version="1.0.0", provides={"memory": "1.0.0"}, requires={"memory": ">=1"})
async def recall_b(ctx: PluginContext) -> None:
    ctx.require("memory")


@plugin(name="cache", version="1.0.0", provides={"cache": "1.0.0"})
async def cache_provider(ctx: PluginContext) -> None:
    from chassis import CapabilityKey

    ctx.capabilities.provide(CapabilityKey.from_version("cache", "1.0.0"), "cache-handle")
