"""Publication validates actual registrations against promised contracts (R004).

A provider that declares a capability but registers nothing must not publish —
including in the first generation, where no prior live registration exists to
compare against — and a consumer whose selected provider cannot satisfy its
requirement rolls the entire candidate back.
"""

from __future__ import annotations

import pytest

from chassis import Harness, PluginContext, plugin
from chassis.capabilities import DATABASE, CapabilityKey
from chassis.core.errors import ConfigurationError, PluginContractError


def liar(name: str, capability: str):  # type: ignore[no-untyped-def]
    @plugin(name=name, version="1.0.0", provides={capability: "1.0.0"})
    async def provide(ctx: PluginContext) -> None:
        return None  # declares the contract and registers nothing

    return provide


def consumer_of(capability: str, requirement: str = "1.0.0"):  # type: ignore[no-untyped-def]
    @plugin(name="consumer", version="1.0.0", requires={capability: requirement})
    async def consume(ctx: PluginContext) -> None:
        return None

    return consume


def good_provider():  # type: ignore[no-untyped-def]
    @plugin(name="good", version="1.0.0", provides={"database": "1.0.0"})
    async def provide(ctx: PluginContext) -> None:
        ctx.capabilities.provide(DATABASE, "db")

    return provide


def drifty_provider():  # type: ignore[no-untyped-def]
    """Promises database@1 but registers a version no consumer accepted."""

    @plugin(name="drifty", version="1.0.0", provides={"database": "1.0.0"})
    async def provide(ctx: PluginContext) -> None:
        ctx.capabilities.provide(DATABASE, "db", version="1.5.0")

    return provide


def wrong_generation_provider():  # type: ignore[no-untyped-def]
    """Promises database@1 but registers the database@2 contract."""

    @plugin(name="wrong-gen", version="1.0.0", provides={"database": "1.0.0"})
    async def provide(ctx: PluginContext) -> None:
        ctx.capabilities.provide(
            CapabilityKey.from_version("database", "2.0.0"), "db", version="2.0.0"
        )

    return provide


async def test_a_provider_that_registers_nothing_cannot_publish_the_first_generation() -> None:
    harness = Harness()
    harness.install(liar("liar", "database"), entry_id="liar")
    harness.install(consumer_of("database"), entry_id="consumer")

    try:
        with pytest.raises(PluginContractError) as excinfo:
            await harness.start()

        context = excinfo.value.context
        assert context["provider"] == "liar"
        assert context["promised"] == "database@1"
        assert context["actual"] == []
        assert context["consumers"] == ["consumer"]
        assert context["rolled_back"] == ["consumer", "liar"]
        assert harness.current_generation is None
    finally:
        await harness.stop()


async def test_a_failed_validation_rolls_back_and_keeps_the_live_generation() -> None:
    harness = Harness()
    harness.install(good_provider(), entry_id="db")
    harness.install(consumer_of("database"), entry_id="consumer")
    await harness.start()
    current = harness.current_generation
    assert current is not None
    previous_instance = harness.plugin_registry.instance("db")
    assert previous_instance is not None

    try:
        harness.install(liar("liar", "database"), entry_id="db", replace=True)
        with pytest.raises(PluginContractError) as excinfo:
            await harness.reconcile()

        # The consumer is rebuilt because its provider's identity changed, so
        # both candidate nodes roll back.
        assert excinfo.value.context["rolled_back"] == ["consumer", "db"]
        # Only the candidate was rolled back: the live generation and the
        # instance it owns are untouched.
        assert harness.current_generation is current
        assert harness.plugin_registry.instance("db") is previous_instance
        assert previous_instance.state.value == "active"
    finally:
        await harness.stop()


async def test_a_registered_version_no_consumer_accepted_rolls_back() -> None:
    harness = Harness()
    harness.install(drifty_provider(), entry_id="drifty")
    harness.install(consumer_of("database", "==1.0.0"), entry_id="consumer")

    try:
        with pytest.raises(PluginContractError) as excinfo:
            await harness.start()

        context = excinfo.value.context
        assert context["provider"] == "drifty"
        assert context["promised"] == "database ==1.0.0"
        assert context["actual"] == ["database@1 1.5.0"]
        assert context["consumers"] == ["consumer"]
        assert harness.current_generation is None
    finally:
        await harness.stop()


async def test_a_registration_on_the_wrong_contract_generation_rolls_back() -> None:
    harness = Harness()
    harness.install(wrong_generation_provider(), entry_id="wrong-gen")

    try:
        with pytest.raises(PluginContractError) as excinfo:
            await harness.start()

        context = excinfo.value.context
        assert context["provider"] == "wrong-gen"
        assert context["promised"] == "database@1"
        assert context["actual"] == ["database@2 2.0.0"]
        assert harness.current_generation is None
    finally:
        await harness.stop()


async def test_consistent_providers_publish_normally() -> None:
    harness = Harness()
    harness.install(good_provider(), entry_id="db")
    harness.install(consumer_of("database"), entry_id="consumer")

    try:
        result = await harness.start()
        assert result.generation_id
        assert harness.current_generation is not None
    finally:
        await harness.stop()


async def test_duplicate_tool_names_within_one_generation_are_rejected() -> None:
    from langchain_core.tools import tool as langchain_tool

    def tool_owner(owner: str):  # type: ignore[no-untyped-def]
        @langchain_tool
        def fetch(url: str) -> str:
            """Fetch a URL."""

            return owner

        @plugin(name=owner, version="1.0.0")
        async def provide(ctx: PluginContext) -> None:
            ctx.tools.register(fetch)

        return provide

    harness = Harness()
    harness.install(tool_owner("owner-a"), entry_id="owner-a")
    harness.install(tool_owner("owner-b"), entry_id="owner-b")

    try:
        with pytest.raises(ConfigurationError) as excinfo:
            await harness.start()

        assert excinfo.value.context["tool"] == "fetch"
        assert harness.current_generation is None
    finally:
        await harness.stop()
