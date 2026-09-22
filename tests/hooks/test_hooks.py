from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest

from chassis.core.errors import HookExecutionError, ScopeClosedError
from chassis.core.scope import Scope
from chassis.hooks import (
    HookErrorPolicy,
    HookEvent,
    HookMode,
    HookRegistry,
)


def recorder(log: list[str], label: str):  # type: ignore[no-untyped-def]
    async def handler(payload: Mapping[str, Any]) -> None:
        log.append(f"{label}:{payload.get('value', '')}")

    return handler


async def test_handlers_run_in_priority_then_registration_order() -> None:
    order: list[str] = []
    registry = HookRegistry()
    scope = Scope("hooks")

    registry.register(
        scope=scope, event=HookEvent.PLUGIN_MOUNTED, handler=recorder(order, "default-a")
    )
    registry.register(
        scope=scope,
        event=HookEvent.PLUGIN_MOUNTED,
        handler=recorder(order, "high"),
        priority=-10,
    )
    registry.register(
        scope=scope,
        event=HookEvent.PLUGIN_MOUNTED,
        handler=recorder(order, "default-b"),
    )
    registry.register(
        scope=scope, event=HookEvent.PLUGIN_MOUNTED, handler=recorder(order, "low"), priority=10
    )

    result = await registry.dispatch(HookEvent.PLUGIN_MOUNTED)

    assert order == ["high:", "default-a:", "default-b:", "low:"]
    assert result.ok
    assert result.stopped is False


async def test_observe_hooks_ignore_their_return_value() -> None:
    async def handler(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"value": "ignored"}

    registry = HookRegistry()
    registry.register(scope=Scope("hooks"), event=HookEvent.PLUGIN_MOUNTED, handler=handler)

    result = await registry.dispatch(HookEvent.PLUGIN_MOUNTED, {"value": "original"})

    assert result.payload["value"] == "original"


async def test_transform_hooks_chain_payload_updates() -> None:
    async def first(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"value": payload["value"] + "-first"}

    async def second(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"value": payload["value"] + "-second"}

    registry = HookRegistry()
    scope = Scope("hooks")
    registry.register(
        scope=scope, event=HookEvent.BEFORE_TOOL_EXECUTE, handler=first, mode=HookMode.TRANSFORM
    )
    registry.register(
        scope=scope, event=HookEvent.BEFORE_TOOL_EXECUTE, handler=second, mode=HookMode.TRANSFORM
    )

    result = await registry.dispatch(HookEvent.BEFORE_TOOL_EXECUTE, {"value": "start"})

    assert result.payload["value"] == "start-first-second"


async def test_transform_hook_returning_none_leaves_payload_untouched() -> None:
    async def noop(payload: Mapping[str, Any]) -> None:
        return None

    registry = HookRegistry()
    registry.register(
        scope=Scope("hooks"),
        event=HookEvent.BEFORE_TOOL_EXECUTE,
        handler=noop,
        mode=HookMode.TRANSFORM,
    )

    result = await registry.dispatch(HookEvent.BEFORE_TOOL_EXECUTE, {"value": "start"})

    assert result.payload["value"] == "start"


async def test_bail_hook_stops_the_chain() -> None:
    order: list[str] = []

    async def stop(payload: Mapping[str, Any]) -> bool:
        return True

    registry = HookRegistry()
    scope = Scope("hooks")
    registry.register(
        scope=scope,
        event=HookEvent.BEFORE_TOOL_EXECUTE,
        handler=stop,
        mode=HookMode.BAIL,
        priority=0,
    )
    registry.register(
        scope=scope,
        event=HookEvent.BEFORE_TOOL_EXECUTE,
        handler=recorder(order, "after"),
        priority=5,
    )

    result = await registry.dispatch(HookEvent.BEFORE_TOOL_EXECUTE)

    assert result.stopped is True
    assert order == []


async def test_failing_handler_is_recorded_by_default() -> None:
    async def broken(payload: Mapping[str, Any]) -> None:
        raise RuntimeError("hook exploded")

    registry = HookRegistry()
    registry.register(scope=Scope("hooks"), event=HookEvent.PLUGIN_MOUNTED, handler=broken)

    result = await registry.dispatch(HookEvent.PLUGIN_MOUNTED)

    assert result.ok is False
    assert "hook exploded" in str(result.failures[0].error)


async def test_failing_handler_can_fail_loudly() -> None:
    async def broken(payload: Mapping[str, Any]) -> None:
        raise RuntimeError("hook exploded")

    registry = HookRegistry()
    registry.register(
        scope=Scope("hooks"),
        event=HookEvent.BEFORE_TOOL_EXECUTE,
        handler=broken,
        error_policy=HookErrorPolicy.RAISE,
    )

    with pytest.raises(HookExecutionError) as excinfo:
        await registry.dispatch(HookEvent.BEFORE_TOOL_EXECUTE)

    assert excinfo.value.context["event"] == "before_tool_execute"
    assert isinstance(excinfo.value.__cause__, RuntimeError)


async def test_registration_is_removed_when_its_scope_closes() -> None:
    registry = HookRegistry()
    scope = Scope("owner")
    registration = registry.register(
        scope=scope, event=HookEvent.PLUGIN_MOUNTED, handler=recorder([], "x")
    )

    assert len(registry) == 1
    assert scope.effects[0].kind == "hook"

    await scope.aclose()

    assert len(registry) == 0
    assert registry.unregister(registration.registration_id) is False


async def test_registration_after_scope_close_is_rejected() -> None:
    registry = HookRegistry()
    scope = Scope("owner")
    await scope.aclose()

    with pytest.raises(ScopeClosedError):
        registry.register(scope=scope, event=HookEvent.PLUGIN_MOUNTED, handler=recorder([], "x"))


async def test_snapshot_restricts_dispatch_to_a_generations_owners() -> None:
    order: list[str] = []
    registry = HookRegistry()
    owner_one = Scope("one")
    owner_two = Scope("two")
    registry.register(
        scope=owner_one,
        event=HookEvent.PLUGIN_MOUNTED,
        handler=recorder(order, "one"),
        owner_id="plugin_1",
    )
    registry.register(
        scope=owner_two,
        event=HookEvent.PLUGIN_MOUNTED,
        handler=recorder(order, "two"),
        owner_id="plugin_2",
    )

    snapshot = registry.snapshot("gen_0002", owner_ids=["plugin_2"])
    await registry.dispatch(HookEvent.PLUGIN_MOUNTED, hooks=snapshot)

    assert order == ["two:"]
    assert len(snapshot) == 1
    assert (
        snapshot.handlers(HookEvent.PLUGIN_MOUNTED)
        == registry.handlers(HookEvent.PLUGIN_MOUNTED)[1:]
    )

    empty = registry.snapshot("gen_0003", owner_ids=[])
    await registry.dispatch(HookEvent.PLUGIN_MOUNTED, hooks=empty)
    assert order == ["two:"]


async def test_dispatch_ignores_events_without_handlers() -> None:
    registry = HookRegistry()

    result = await registry.dispatch(HookEvent.GENERATION_PUBLISHED, {"generation": "gen_0001"})

    assert result.payload["generation"] == "gen_0001"
    assert result.ok


async def test_payload_is_immutable_for_handlers() -> None:
    seen: list[bool] = []

    async def handler(payload: Mapping[str, Any]) -> None:
        with pytest.raises(TypeError):
            payload["mutated"] = True  # type: ignore[index]
        seen.append(True)

    registry = HookRegistry()
    registry.register(scope=Scope("hooks"), event=HookEvent.PLUGIN_MOUNTED, handler=handler)

    await registry.dispatch(HookEvent.PLUGIN_MOUNTED, {"value": 1})

    assert seen == [True]


async def test_diagnostics_describe_handlers_without_payloads() -> None:
    registry = HookRegistry()
    registry.register(
        scope=Scope("hooks"),
        event=HookEvent.POLICY_DECISION,
        handler=recorder([], "audit"),
        owner_id="plugin_audit",
    )

    payload = registry.to_dict()["hooks"][0]

    assert payload["event"] == "policy_decision"
    assert payload["owner_id"] == "plugin_audit"
    assert "secret" not in str(payload)


# --------------------------------------------------------------------------
# Hook semantics (R017): TRANSFORM replaces, payloads freeze, failures surface.
# --------------------------------------------------------------------------


async def test_transform_replaces_the_payload_and_can_remove_keys() -> None:
    registry = HookRegistry()
    scope = Scope("owner")

    async def replace(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"kept": 2}

    registry.register(
        scope=scope,
        event=HookEvent.BEFORE_TOOL_EXECUTE,
        handler=replace,
        mode=HookMode.TRANSFORM,
    )
    result = await registry.dispatch(HookEvent.BEFORE_TOOL_EXECUTE, {"kept": 1, "removed": "gone"})

    # Replacement semantics: a key the transform does not carry is removed.
    assert dict(result.payload) == {"kept": 2}


async def test_chained_transforms_each_replace_the_payload() -> None:
    registry = HookRegistry()
    scope = Scope("owner")
    seen: list[dict[str, Any]] = []

    async def first(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"a": 1, "b": 2}

    async def second(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        seen.append(dict(payload))
        return {"a": 9}

    registry.register(
        scope=scope,
        event=HookEvent.BEFORE_TOOL_EXECUTE,
        handler=first,
        mode=HookMode.TRANSFORM,
        priority=-10,
    )
    registry.register(
        scope=scope,
        event=HookEvent.BEFORE_TOOL_EXECUTE,
        handler=second,
        mode=HookMode.TRANSFORM,
    )
    result = await registry.dispatch(HookEvent.BEFORE_TOOL_EXECUTE, {"a": 0, "z": 3})

    assert seen == [{"a": 1, "b": 2}]
    assert dict(result.payload) == {"a": 9}


async def test_hook_payloads_are_deeply_frozen() -> None:
    registry = HookRegistry()
    scope = Scope("owner")

    async def observe(payload: Mapping[str, Any]) -> None:
        with pytest.raises(TypeError):
            payload["nested"]["x"] = 1  # type: ignore[index]
        return None

    registry.register(
        scope=scope,
        event=HookEvent.BEFORE_TOOL_EXECUTE,
        handler=observe,
        mode=HookMode.OBSERVE,
    )
    result = await registry.dispatch(
        HookEvent.BEFORE_TOOL_EXECUTE, {"nested": {"x": 0, "rows": [{"y": 1}]}}
    )

    with pytest.raises(TypeError):
        result.payload["nested"]["rows"][0]["y"] = 2  # type: ignore[index]


async def test_recorded_control_plane_failures_do_not_abort_the_transition() -> None:
    from chassis import Harness, PluginContext, plugin

    async def boom(payload: Mapping[str, Any]) -> None:
        raise RuntimeError("observer exploded")

    @plugin(name="observer", version="1.0.0")
    async def observer(ctx: PluginContext) -> None:
        ctx.hooks.register(HookEvent.PLUGIN_MOUNTED, boom)

    harness = Harness()
    harness.install(observer, entry_id="observer")
    result = await harness.reconcile()
    try:
        assert any("boom" in failure.description for failure in result.failures)
    finally:
        await harness.stop()


async def test_recorded_data_plane_failures_surface_without_failing_the_operation() -> None:
    from chassis import PluginContext, plugin
    from chassis.runtime import AgentEvent, AgentRequest, AgentResult, HarnessRunContext
    from chassis.telemetry import RecordingTelemetry
    from chassis.testing import TestHarness, fake_tool
    from chassis.tools import ToolRequest

    telemetry = RecordingTelemetry()

    async def boom(payload: Mapping[str, Any]) -> None:
        raise RuntimeError("observer exploded")

    @plugin(name="observer", version="1.0.0")
    async def observer(ctx: PluginContext) -> None:
        ctx.hooks.register(HookEvent.BEFORE_TOOL_EXECUTE, boom)
        ctx.hooks.register(HookEvent.BEFORE_AGENT_RUN, boom)

    class TinyRuntime:
        @property
        def name(self) -> str:
            return "tiny"

        async def invoke(
            self, request: AgentRequest, run_context: HarnessRunContext
        ) -> AgentResult:
            return AgentResult(
                agent="tiny",
                generation_id=run_context.generation_id,
                run_id=run_context.run_id,
            )

        async def stream(
            self, request: AgentRequest, run_context: HarnessRunContext
        ) -> AsyncIterator[AgentEvent]:
            yield AgentEvent(
                agent="tiny",
                generation_id=run_context.generation_id,
                run_id=run_context.run_id,
                kind="end",
            )

    async with TestHarness(telemetry=telemetry) as harness:
        harness.install_tools(fake_tool("echo", result="ok", parameters={"text": (str, ...)}))
        harness.install(observer, entry_id="observer")
        harness.register_agent(TinyRuntime())
        await harness.reconcile()
        generation = harness.current_generation
        assert generation is not None

        result = await harness.tool_executor.execute(
            ToolRequest(name="echo", args={"text": "hi"}),
            snapshot=harness.tool_snapshot(generation),
        )
        run = await harness.agents.invoke("tiny", {"messages": []})

        # Neither operation failed on the observer's account.
        assert result.ok
        assert run.agent == "tiny"

        failures = [event for event in harness.telemetry.events if event.name == "hook.failure"]
        assert len(failures) == 2
        for failure in failures:
            assert failure.attributes["error_type"] == "RuntimeError"
            assert "exploded" not in str(failure.attributes)
