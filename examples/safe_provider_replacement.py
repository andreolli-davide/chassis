"""Example C --- safe provider replacement.

    Generation 1: provider A        run A starts
                                    provider changes
    Generation 2: provider B        run B uses generation 2
                                    run A keeps using generation 1
                                    run A completes
                                    generation 1 drains
                                    provider A is disposed

The point is what does *not* happen: run A never observes provider B, provider A
is never disposed while run A can still reach it, and provider B is never torn
down when generation 1 retires.

Run it with::

    uv run python examples/safe_provider_replacement.py
"""

from __future__ import annotations

import asyncio

from chassis import DATABASE, Harness, PluginContext, plugin


class Provider:
    """A provider that fails loudly if it is used after disposal."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.disposed = False

    def query(self) -> str:
        if self.disposed:
            raise RuntimeError(f"{self.name} was used after disposal")
        return f"answer from {self.name}"


def provider_plugin(name: str):  # type: ignore[no-untyped-def]
    @plugin(name=name, version="1.0.0", provides={"database": "1.0.0"})
    async def provider(ctx: PluginContext) -> None:
        value = Provider(name)
        ctx.capabilities.provide(DATABASE, value)
        ctx.cleanup(
            f"dispose {name}",
            lambda: (
                setattr(value, "disposed", True),
                print(f"    ({name} disposed)"),
            ),
        )

    return provider


async def main() -> None:
    harness = Harness(name="replacement-demo")
    harness.install(provider_plugin("provider-a"), entry_id="db")
    await harness.start()

    generation_one = harness.current_generation
    assert generation_one is not None
    provider_a: Provider = generation_one.snapshot.require(DATABASE)
    instance_a = harness.plugin_registry.instance("db")
    print(f"[start] {generation_one.generation_id} uses {provider_a.name}")
    print(f"[start] leases={generation_one.lease_count} instances={len(harness.plugin_registry.instances())}")

    # Run A acquires generation 1 and holds it across the provider change.
    run_a_entered = asyncio.Event()
    run_a_finish = asyncio.Event()
    observed: dict[str, str] = {}

    async def run_a() -> None:
        async with harness.acquire() as generation:
            provider: Provider = generation.snapshot.require(DATABASE)
            observed["generation"] = generation.generation_id
            run_a_entered.set()
            await run_a_finish.wait()
            observed["answer"] = provider.query()

    run_a_task = asyncio.create_task(run_a())
    await run_a_entered.wait()
    print(f"\n[run A] holding {observed['generation']}, leases={generation_one.lease_count}")

    # Swap the provider.
    print("[change] provider replaced")
    harness.install(provider_plugin("provider-b"), entry_id="db", replace=True)
    result = await harness.reconcile()
    generation_two = harness.current_generation
    assert generation_two is not None

    print(f"[publish] new generation {result.generation_id}, previous {generation_one.generation_id}")
    print(f"[publish] states: gen1={generation_one.state.value} gen2={generation_two.state.value}")
    assert generation_one.state.value == "draining"
    assert generation_two.snapshot.require(DATABASE).name == "provider-b"

    # Provider A must still be alive: a live generation can reach it.
    assert provider_a.disposed is False
    assert instance_a is not None and instance_a.state.value == "active"
    print(f"[safety] provider-a alive={not provider_a.disposed}, generation_refs={instance_a.generation_refs}")

    # A new run observes generation 2 only.
    async with harness.acquire() as new_generation:
        provider_b: Provider = new_generation.snapshot.require(DATABASE)
        print(f"\n[run B] {new_generation.generation_id} uses {provider_b.name}: {provider_b.query()}")
        assert provider_b.name == "provider-b"

    # Run A finishes on the generation it acquired.
    run_a_finish.set()
    await run_a_task
    print(f"\n[run A] completed on {observed['generation']}: {observed['answer']}")
    assert observed["answer"] == "answer from provider-a"

    # Its generation drained, so the now-unreachable provider is disposed;
    # provider B is untouched because the current generation still reaches it.
    print(f"[drain] gen1 state={generation_one.state.value} leases={generation_one.lease_count}")
    assert generation_one.state.value == "retired"
    assert provider_a.disposed is True
    assert generation_two.snapshot.require(DATABASE).name == "provider-b"
    assert provider_b.disposed is False
    print(f"[dispose] provider-a disposed={provider_a.disposed} provider-b disposed={provider_b.disposed}")

    print("\n[generations]")
    for generation in harness.generation_manager.all_generations():
        print(
            f"  {generation.generation_id} state={generation.state.value} "
            f"leases={generation.lease_count} instances={generation.instance_ids}"
        )

    await harness.stop()
    print("\nExample C completed: active runs kept their generation, and only")
    print("unreachable resources were disposed.")


if __name__ == "__main__":
    asyncio.run(main())
