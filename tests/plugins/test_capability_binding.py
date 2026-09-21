"""Exact capability contract binding (roadmap R007).

Requirements bind the exact selected contract — the requirement predicate
decides, never registration order or a random id — multi-contract providers
serve each contract, and application provisions are keyed by the full
``CapabilityKey`` so v1 and v2 can coexist.
"""

from __future__ import annotations

from typing import Any

from chassis import Harness, PluginContext, plugin
from chassis.capabilities import CapabilityKey

DATABASE_V1 = CapabilityKey("database", "1")
DATABASE_V2 = CapabilityKey("database", "2")
MEMORY = CapabilityKey("memory", "1")
MODEL = CapabilityKey("model", "1")


async def test_a_multi_contract_provider_binds_each_consumer_to_its_exact_contract() -> None:
    seen: dict[str, Any] = {}

    @plugin(name="dual", version="1.0.0", provides={"database": ("1.0.0", "2.0.0")})
    async def dual(ctx: PluginContext) -> None:
        ctx.capabilities.provide(DATABASE_V1, "db-v1", version="1.0.0")
        ctx.capabilities.provide(DATABASE_V2, "db-v2", version="2.0.0")

    @plugin(name="consumer-v1", version="1.0.0", requires={"database": ">=1,<2"})
    async def consumer_v1(ctx: PluginContext) -> None:
        seen["v1"] = ctx.require(DATABASE_V1)

    @plugin(name="consumer-v2", version="1.0.0", requires={"database": ">=2,<3"})
    async def consumer_v2(ctx: PluginContext) -> None:
        seen["v2"] = ctx.require(DATABASE_V2)

    harness = Harness()
    harness.install(dual, entry_id="dual")
    harness.install(consumer_v1, entry_id="consumer-v1")
    harness.install(consumer_v2, entry_id="consumer-v2")
    await harness.start()
    try:
        assert seen == {"v1": "db-v1", "v2": "db-v2"}
    finally:
        await harness.stop()


async def test_an_open_multi_major_range_is_generation_neutral() -> None:
    seen: list[Any] = []

    @plugin(name="provider", version="1.0.0", provides={"database": "2.1.0"})
    async def provider(ctx: PluginContext) -> None:
        ctx.capabilities.provide(DATABASE_V2, "db", version="2.1.0")

    @plugin(name="consumer", version="1.0.0", requires={"database": ">1.9,<3"})
    async def consumer(ctx: PluginContext) -> None:
        seen.append(ctx.require(CapabilityKey("database", "2")))

    harness = Harness()
    harness.install(provider, entry_id="provider")
    harness.install(consumer, entry_id="consumer")
    await harness.start()
    try:
        # The range accepts 2.x, so it must not be pinned to the @1 generation.
        assert seen == ["db"]
        plan = harness.plan()
        resolution = plan.plan_for("consumer").requirements[0]  # type: ignore[union-attr]
        assert resolution.provider_key == "database@2"
    finally:
        await harness.stop()


async def test_binding_honors_the_requirement_predicate() -> None:
    seen: list[Any] = []

    @plugin(name="picky", version="1.0.0", provides={"database": "1.0.0"})
    async def picky(ctx: PluginContext) -> None:
        # The excluded version registers first: binding must skip it.
        ctx.capabilities.provide(DATABASE_V1, "excluded", version="1.5.0")
        ctx.capabilities.provide(DATABASE_V1, "allowed", version="1.4.0")

    @plugin(name="consumer", version="1.0.0", requires={"database": ">=1,<2,!=1.5.0"})
    async def consumer(ctx: PluginContext) -> None:
        seen.append(ctx.require(DATABASE_V1))

    harness = Harness()
    harness.install(picky, entry_id="picky")
    harness.install(consumer, entry_id="consumer")
    await harness.start()
    try:
        assert seen == ["allowed"]
        plan = harness.plan()
        resolution = plan.plan_for("consumer").requirements[0]  # type: ignore[union-attr]
        assert resolution.provider_key == "database@1"
        assert resolution.provider_version == "1.4.0"
    finally:
        await harness.stop()


async def test_disjoint_ranges_report_version_mismatch_not_absence() -> None:
    @plugin(name="provider", version="1.0.0", provides={"database": "1.0.0"})
    async def provider(ctx: PluginContext) -> None:
        ctx.capabilities.provide(DATABASE_V1, "db", version="1.0.0")

    @plugin(name="consumer-hi", version="1.0.0", requires={"database": ">=3,<4"})
    async def consumer_hi(ctx: PluginContext) -> None:
        pass

    harness = Harness()
    harness.install(provider, entry_id="provider")
    harness.install(consumer_hi, entry_id="consumer-hi")
    await harness.start()
    try:
        plan = harness.plan()
        resolution = plan.plan_for("consumer-hi").requirements[0]  # type: ignore[union-attr]
        assert resolution.status == "version_mismatch"
    finally:
        await harness.stop()


async def test_simultaneous_v1_and_v2_provisions_coexist() -> None:
    seen: dict[str, Any] = {}

    @plugin(name="consumer-v1", version="1.0.0", requires={"database": ">=1,<2"})
    async def consumer_v1(ctx: PluginContext) -> None:
        seen["v1"] = ctx.require(DATABASE_V1)

    @plugin(name="consumer-v2", version="1.0.0", requires={"database": ">=2,<3"})
    async def consumer_v2(ctx: PluginContext) -> None:
        seen["v2"] = ctx.require(DATABASE_V2)

    harness = Harness()
    harness.provide(DATABASE_V1, "app-v1", version="1.0.0")
    harness.provide(DATABASE_V2, "app-v2", version="2.0.0")
    harness.install(consumer_v1, entry_id="consumer-v1")
    harness.install(consumer_v2, entry_id="consumer-v2")
    await harness.start()
    try:
        assert seen == {"v1": "app-v1", "v2": "app-v2"}
    finally:
        await harness.stop()


async def test_snapshot_registration_order_is_deterministic() -> None:
    def triple_provider():  # type: ignore[no-untyped-def]
        @plugin(
            name="triple",
            version="1.0.0",
            provides={"model": "1.0.0", "database": "1.0.0", "memory": "1.0.0"},
        )
        async def provide(ctx: PluginContext) -> None:
            ctx.capabilities.provide(MODEL, "m", version="1.0.0")
            ctx.capabilities.provide(DATABASE_V1, "d", version="1.0.0")
            ctx.capabilities.provide(MEMORY, "x", version="1.0.0")

        return provide

    orders: list[tuple[str, ...]] = []
    for _ in range(5):
        harness = Harness()
        harness.install(triple_provider(), entry_id="triple")
        await harness.start()
        try:
            generation = harness.current_generation
            assert generation is not None
            orders.append(tuple(str(item.key) for item in generation.snapshot.registrations))
        finally:
            await harness.stop()

    assert len(set(orders)) == 1
    assert orders[0] == ("database@1", "memory@1", "model@1") or orders[0] == (
        "model@1",
        "database@1",
        "memory@1",
    )
