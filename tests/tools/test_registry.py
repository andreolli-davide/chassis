from __future__ import annotations

import pytest
from langchain_core.tools import BaseTool, tool

from chassis import Harness, PluginContext, plugin
from chassis.capabilities import SECRETS, CapabilityKey
from chassis.core.errors import ConfigurationError, ScopeClosedError
from chassis.core.scope import Scope
from chassis.hooks import HookEvent
from chassis.secrets import StaticSecretProvider
from chassis.tools import RegisteredTool, ToolNotFound, ToolPolicy, ToolRegistry

TOOLS = CapabilityKey("tools", "1")


@tool
def add_numbers(a: int, b: int) -> int:
    """Add two integers."""

    return a + b


@tool
def echo(text: str) -> str:
    """Echo the input."""

    return text


def test_registration_wraps_a_langchain_tool_without_replacing_it() -> None:
    registry = ToolRegistry()
    scope = Scope("owner")

    entry = registry.register(
        scope=scope,
        tool=add_numbers,
        policy=ToolPolicy(permissions=("network.fetch",), idempotent=True),
        owner_id="plugin_1",
        owner_name="math",
    )

    assert isinstance(entry, RegisteredTool)
    assert isinstance(entry.tool, BaseTool)
    assert entry.tool is add_numbers
    assert entry.name == "add_numbers"
    assert entry.policy.idempotent is True
    assert entry.policy.permission_objects[0].name == "network.fetch"
    assert scope.effects[0].kind == "tool"


async def test_registration_is_reverted_when_the_scope_closes() -> None:
    registry = ToolRegistry()
    scope = Scope("owner")
    registry.register(scope=scope, tool=echo)

    assert registry.names() == ("echo",)

    await scope.aclose()

    assert registry.names() == ()
    assert registry.get("echo") is None


async def test_registration_after_scope_close_is_rejected() -> None:
    registry = ToolRegistry()
    scope = Scope("owner")
    await scope.aclose()

    with pytest.raises(ScopeClosedError):
        registry.register(scope=scope, tool=echo)


def test_duplicate_and_invalid_registrations_are_rejected() -> None:
    registry = ToolRegistry()
    scope = Scope("owner")
    registry.register(scope=scope, tool=echo, owner_name="first")

    with pytest.raises(ConfigurationError) as excinfo:
        registry.register(scope=scope, tool=echo, owner_name="second")

    assert excinfo.value.context["owner"] == "first"

    with pytest.raises(ConfigurationError):
        registry.register(scope=scope, tool="not a tool")  # type: ignore[arg-type]


def test_snapshot_is_restricted_to_a_generations_owners_and_sorted() -> None:
    registry = ToolRegistry()
    owner_one = Scope("one")
    owner_two = Scope("two")
    registry.register(scope=owner_one, tool=echo, owner_id="plugin_1")
    registry.register(scope=owner_two, tool=add_numbers, owner_id="plugin_2")

    snapshot = registry.snapshot("gen_0002", owner_ids=["plugin_1"])

    assert snapshot.names == ("echo",)
    assert len(snapshot) == 1
    assert "echo" in snapshot
    assert snapshot.require("echo").owner_id == "plugin_1"

    every = registry.snapshot("gen_0002")
    assert every.names == ("add_numbers", "echo")
    assert [item.name for item in every.to_tools()] == ["add_numbers", "echo"]

    empty = registry.snapshot("gen_0003", owner_ids=[])
    assert len(empty) == 0
    assert "echo" not in empty


def test_snapshot_require_reports_the_generation_and_available_tools() -> None:
    registry = ToolRegistry()
    scope = Scope("owner")
    registry.register(scope=scope, tool=echo)
    snapshot = registry.snapshot("gen_0007")

    with pytest.raises(ToolNotFound) as excinfo:
        snapshot.require("missing")

    assert excinfo.value.context["generation_id"] == "gen_0007"
    assert excinfo.value.context["available"] == ["echo"]


def test_diagnostics_describe_tools_without_the_tool_payload() -> None:
    registry = ToolRegistry()
    scope = Scope("owner")
    registry.register(
        scope=scope, tool=echo, policy=ToolPolicy(cost_class="cheap"), owner_name="utils"
    )

    payload = registry.to_dict()["tools"][0]

    assert payload["name"] == "echo"
    assert payload["policy"]["cost_class"] == "cheap"
    assert payload["owner_name"] == "utils"
    assert "func" not in str(payload)


async def test_plugin_registers_tools_through_its_own_scope() -> None:
    @plugin(name="utils", version="1.0.0", provides={"tools": "1.0.0"})
    async def utils(ctx: PluginContext) -> None:
        ctx.tools.register(echo, policy=ToolPolicy(permissions=("network.fetch",)))
        ctx.tools.register(add_numbers)
        ctx.capabilities.provide(TOOLS, "utils")

    harness = Harness()
    harness.install(utils, entry_id="utils")
    await harness.start()
    try:
        instance = harness.plugin_registry.instance("utils")
        assert instance is not None
        assert harness.tools.names() == ("add_numbers", "echo")

        generation = harness.current_generation
        assert generation is not None
        snapshot = harness.tool_snapshot(generation)
        assert snapshot.names == ("add_numbers", "echo")
        assert snapshot.require("echo").owner_id == instance.instance_id
    finally:
        await harness.stop()

    assert harness.tools.names() == ()


async def test_plugin_hooks_are_scope_owned() -> None:
    seen: list[str] = []

    @plugin(name="audit", version="1.0.0")
    async def audit(ctx: PluginContext) -> None:
        async def handler(payload: object) -> None:
            seen.append("observed")

        ctx.hooks.register(HookEvent.PLUGIN_MOUNTED, handler)

    harness = Harness()
    harness.install(audit, entry_id="audit")
    await harness.start()
    try:
        # The mount boundary reaches the handler the plugin registered during its
        # own setup: registrations are live as soon as the owning scope exists.
        assert len(harness.hooks) == 1
        assert seen == ["observed"]

        generation = harness.current_generation
        assert generation is not None
        assert len(harness.hook_snapshot(generation)) == 1
    finally:
        await harness.stop()

    assert len(harness.hooks) == 0


async def test_plugin_reads_secrets_through_its_context() -> None:
    """Secrets are read through the provider abstraction, never the environment."""

    seen: list[str] = []

    @plugin(name="reader", version="1.0.0", requires={"secrets": ">=1,<2"})
    async def reader(ctx: PluginContext) -> None:
        seen.append((await ctx.secrets.get("openai.api_key")).reveal())

    @plugin(name="vault", version="1.0.0", provides={"secrets": "1.0.0"})
    async def vault(ctx: PluginContext) -> None:
        ctx.capabilities.provide(SECRETS, StaticSecretProvider({"openai.api_key": "value-1234"}))

    harness = Harness()
    harness.install(vault, entry_id="vault")
    harness.install(reader, entry_id="reader")
    await harness.start()
    try:
        assert seen == ["value-1234"]
    finally:
        await harness.stop()


async def test_plugin_without_a_secrets_capability_uses_the_harness_provider() -> None:
    seen: list[str] = []

    @plugin(name="reader", version="1.0.0")
    async def reader(ctx: PluginContext) -> None:
        seen.append((await ctx.secrets.get("api.token")).reveal())

    harness = Harness(secrets=StaticSecretProvider({"api.token": "harness-value"}))
    harness.install(reader, entry_id="reader")
    await harness.start()
    try:
        assert seen == ["harness-value"]
    finally:
        await harness.stop()
