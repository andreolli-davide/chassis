from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from chassis import MODEL, PluginContext, plugin
from chassis.capabilities import TOOLS
from chassis.core.errors import CapabilityNotFound, PolicyDenied
from chassis.policy import PolicyRequest
from chassis.secrets import RedactingSecretProvider
from chassis.testing import (
    FakeChatModel,
    FakePolicy,
    FakeSecrets,
    TestHarness,
    fake_tool,
)
from chassis.tools import ToolPolicy


async def test_fake_chat_model_replays_scripted_responses_and_records_calls() -> None:
    model = FakeChatModel(responses=["first", "second"])

    first = await model.ainvoke([HumanMessage("a")])
    second = await model.ainvoke([HumanMessage("b")])
    third = await model.ainvoke([HumanMessage("c")])

    assert first.content == "first"
    assert second.content == "second"
    assert third.content == "second"  # the last scripted response repeats
    assert [message.content for call in model.calls for message in call] == ["a", "b", "c"]


async def test_fake_chat_model_can_emit_tool_calls() -> None:
    scripted = AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "1"}])
    model = FakeChatModel(responses=[scripted])

    response = await model.ainvoke([HumanMessage("a")])

    assert response.tool_calls[0]["name"] == "t"


async def test_fake_tool_records_calls_and_can_fail() -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    tool = fake_tool("echo", result="ok", parameters={"text": (str, ...)}, calls=calls)

    assert await tool.ainvoke({"text": "hi"}) == "ok"
    assert calls == [("echo", {"text": "hi"})]

    broken = fake_tool("boom", error=RuntimeError("nope"), parameters={"x": (int, ...)})
    with pytest.raises(RuntimeError):
        await broken.ainvoke({"x": 1})


async def test_fake_tool_can_delay_for_timeout_tests() -> None:
    tool = fake_tool("slow", result="done", delay_seconds=5, parameters={})

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await tool.ainvoke({})


async def test_fake_policy_records_decisions_and_denies_explicitly() -> None:
    policy = FakePolicy(["network.fetch"], deny=["filesystem.delete"])

    allowed = await policy.evaluate(
        PolicyRequest(
            permission=__import__("chassis.policy", fromlist=["Permission"]).Permission(
                "network.fetch"
            ),
            subject="t",
        )
    )
    denied = await policy.evaluate(
        PolicyRequest(
            permission=__import__("chassis.policy", fromlist=["Permission"]).Permission(
                "filesystem.delete"
            ),
            subject="t",
        )
    )

    assert allowed.allowed
    assert not denied.allowed
    assert policy.denied_permissions == ("filesystem.delete",)


def test_fake_secrets_is_an_in_memory_provider() -> None:
    secrets = FakeSecrets({"openai.api_key": "value-1234"})

    assert secrets.source == "fake"
    assert secrets.names() == ("openai.api_key",)


async def test_test_harness_defaults_are_deterministic() -> None:
    async with TestHarness() as harness:
        assert isinstance(harness.policy, FakePolicy)
        assert isinstance(harness.secrets, RedactingSecretProvider)
        assert isinstance(harness.secrets.inner, FakeSecrets)
        # Starting the harness reconciles once, and that is instrumented.
        assert harness.recorded_spans == ["harness.reconcile"]

        harness.provide_secret("openai.api_key", "value-1234")
        secret = await harness.secrets.get("openai.api_key")
        assert secret.reveal() == "value-1234"


async def test_test_harness_installs_tools_and_reports_them() -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    async with TestHarness(
        tools=[fake_tool("echo", result="ok", parameters={"text": (str, ...)}, calls=calls)],
        tool_policies={"echo": ToolPolicy(permissions=("network.fetch",))},
    ) as harness:
        assert harness.tools.names() == ("echo",)
        assert harness.tools_of().names == ("echo",)
        assert [
            registration.key for registration in harness.capability_registry.registrations()
        ] == [TOOLS]
        assert harness.tools.get("echo").policy.permissions == ("network.fetch",)  # type: ignore[union-attr]


async def test_test_harness_provides_capabilities_that_resolve() -> None:
    async with TestHarness() as harness:
        harness.provide(MODEL, FakeChatModel(responses=["ok"]))
        await harness.start()  # applies the pending provision

        async with harness.acquire() as generation:
            model = generation.snapshot.require(MODEL)

        assert isinstance(model, FakeChatModel)
        assert harness.capability_providers(MODEL) == ("chassis-services",)


async def test_test_harness_can_drive_a_plugin_end_to_end() -> None:
    @plugin(name="consumer", version="1.0.0", requires={"model": ">=1,<2"})
    async def consumer(ctx: PluginContext) -> None:
        ctx.require(MODEL)

    async with TestHarness(plugins=[consumer]) as harness:
        assert harness.plugin_registry.instance("plugin-1") is None

        harness.provide(MODEL, "a model")
        await harness.reconcile()

        assert harness.instance("plugin-1").state.value == "active"


async def test_test_harness_tool_denial_is_observable() -> None:
    from chassis.tools import ToolExecutor, ToolRequest

    async with TestHarness(
        policy=FakePolicy(["network.fetch"]),
        tools=[fake_tool("rm", result="deleted", parameters={})],
        tool_policies={"rm": ToolPolicy(permissions=("filesystem.delete",))},
    ) as harness:
        generation = harness.current_generation
        assert generation is not None

        with pytest.raises(PolicyDenied):
            await ToolExecutor(policy=harness.policy).execute(
                ToolRequest(name="rm", args={}), snapshot=harness.tool_snapshot(generation)
            )


async def test_missing_capability_raises_in_a_test_harness() -> None:
    async with TestHarness() as harness, harness.acquire() as generation:
        with pytest.raises(CapabilityNotFound):
            generation.snapshot.require(MODEL)
