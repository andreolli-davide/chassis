"""Example B --- reactive dependency cascade.

    database -> memory -> agent extension

Removing the database provider must remove everything that depends on it, in
dependency-safe order, and restoring it must let the same plugins reactivate.
Nothing in the example configures that ordering: it is derived from declared
capabilities.

Run it with::

    uv run python examples/reactive_cascade.py
"""

from __future__ import annotations

import asyncio

from chassis import DATABASE, MEMORY, TOOLS, Harness, PluginContext, plugin


def postgres_plugin():  # type: ignore[no-untyped-def]
    @plugin(name="postgres", version="1.0.0", provides={"database": "1.0.0"})
    async def postgres(ctx: PluginContext) -> None:
        connection = {"dsn": "postgres://localhost/app"}
        ctx.capabilities.provide(DATABASE, connection)
        ctx.cleanup("close connection", lambda: print("    (connection closed)"))

    return postgres


def memory_plugin():  # type: ignore[no-untyped-def]
    @plugin(
        name="memory",
        version="1.0.0",
        provides={"memory": "1.0.0"},
        requires={"database": ">=1,<2"},
    )
    async def memory(ctx: PluginContext) -> None:
        database = ctx.require(DATABASE)
        ctx.capabilities.provide(MEMORY, {"store": {"dsn": database["dsn"]}})  # type: ignore[index]
        ctx.cleanup("flush memory store", lambda: print("    (memory flushed)"))

    return memory


def extension_plugin():  # type: ignore[no-untyped-def]
    @plugin(
        name="agent-extension",
        version="1.0.0",
        provides={"tools": "1.0.0"},
        requires={"memory": ">=1,<2"},
    )
    async def extension(ctx: PluginContext) -> None:
        ctx.require(MEMORY)
        ctx.capabilities.provide(TOOLS, ("recall",))
        ctx.cleanup("remove extension tools", lambda: print("    (extension tools removed)"))

    return extension


async def main() -> None:
    harness = Harness(name="cascade-demo")
    harness.install(postgres_plugin(), entry_id="db")
    harness.install(memory_plugin(), entry_id="memory")
    harness.install(extension_plugin(), entry_id="extension")

    # Declared capabilities decide the order, not installation order.
    plan = harness.plan()
    print(f"[plan] activation order: {plan.activation_order}")
    assert plan.activation_order == ("db", "memory", "extension")

    result = await harness.start()
    print(f"[start] generation {result.generation_id} mounted {result.mounted}")
    assert {registration.key for registration in harness.capability_registry.registrations()} == {
        DATABASE,
        MEMORY,
        TOOLS,
    }

    # Remove the provider.
    print("\n[change] database provider removed from desired state")
    harness.uninstall("db")
    cascade = await harness.reconcile()

    print(f"[cascade] pending: {cascade.plan.pending}")
    print(f"[cascade] disposed (consumers first): {cascade.disposed}")
    print(f"[cascade] capabilities left: {len(harness.capability_registry)}")

    assert cascade.disposed == ("extension", "memory", "db")
    assert harness.capability_registry.registrations() == ()
    assert harness.plugin_registry.instances() == ()

    # Diagnostics explain *why* a plugin is inactive, without reading logs.
    print("\n[diagnostics]")
    for line in harness.diagnostics.explain("memory").splitlines():
        print(f"  {line}")

    # Restore the provider.
    print("\n[change] database provider restored")
    harness.install(postgres_plugin(), entry_id="db")
    restored = await harness.reconcile()

    print(f"[restored] activation order: {restored.plan.activation_order}")
    print(f"[restored] mounted: {restored.mounted}")
    assert restored.plan.activation_order == ("db", "memory", "extension")
    assert restored.mounted == ("db", "memory", "extension")

    status = harness.diagnostics.status()
    print(f"[status] composition: {status['composition']}")

    await harness.stop()
    print("\nExample B completed: removal cascaded, restoration reactivated.")


if __name__ == "__main__":
    asyncio.run(main())
