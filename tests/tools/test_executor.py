from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from langchain_core.tools import tool

from chassis.budget import BudgetDimension, BudgetGovernor, BudgetLimits
from chassis.core.errors import BudgetExceeded, PolicyDenied, ToolExecutionError
from chassis.core.scope import Scope
from chassis.hooks import (
    HookErrorPolicy,
    HookEvent,
    HookMode,
    HookRegistry,
)
from chassis.policy import GrantPolicy, Permission
from chassis.secrets import SecretRedactor
from chassis.telemetry import RecordingTelemetry
from chassis.tools import (
    AutoApprove,
    ToolExecutor,
    ToolNotFound,
    ToolPolicy,
    ToolRegistry,
    ToolRequest,
)

SECRET = "sk-live-abcdef123456"


@tool
def add_numbers(a: int, b: int) -> int:
    """Add two integers."""

    return a + b


@tool
def read_secret(name: str) -> str:
    """Return a secret value, used to prove redaction."""

    return SECRET


@tool
def explode(reason: str) -> str:
    """Always fail, including the secret in the failure text."""

    raise RuntimeError(f"boom {SECRET}")


@tool
def slow(seconds: float) -> str:
    """Sleep, then return."""

    import time

    time.sleep(seconds)
    return "done"


async def _async_slow(seconds: float) -> str:
    await asyncio.sleep(seconds)
    return "done"


@tool("slow_async")
async def slow_async(seconds: float) -> str:
    """Sleep asynchronously, then return."""

    return await _async_slow(seconds)


def build_snapshot(*tools: Any, policies: Mapping[str, ToolPolicy] | None = None):  # type: ignore[no-untyped-def]
    registry = ToolRegistry()
    scope = Scope("tools")
    mapping = dict(policies or {})
    for entry in tools:
        registry.register(
            scope=scope, tool=entry, policy=mapping.get(entry.name), owner_name="test-owner"
        )
    return registry.snapshot("gen_0001"), scope


async def test_successful_execution_returns_content() -> None:
    snapshot, _ = build_snapshot(add_numbers)
    executor = ToolExecutor()

    result = await executor.execute(
        ToolRequest(name="add_numbers", args={"a": 2, "b": 3}), snapshot=snapshot
    )

    assert result.ok
    assert result.content == 5
    assert result.artifact is None
    assert result.generation_id == "gen_0001"
    assert result.duration_seconds >= 0


async def test_unknown_tool_is_refused_by_the_harness() -> None:
    snapshot, _ = build_snapshot(add_numbers)

    with pytest.raises(ToolNotFound):
        await ToolExecutor().execute(ToolRequest(name="missing", args={}), snapshot=snapshot)


async def test_policy_denial_refuses_execution() -> None:
    snapshot, _ = build_snapshot(
        read_secret, policies={"read_secret": ToolPolicy(permissions=("secrets.read",))}
    )
    executor = ToolExecutor(policy=GrantPolicy(["network.fetch"]))

    with pytest.raises(PolicyDenied) as excinfo:
        await executor.execute(
            ToolRequest(name="read_secret", args={"name": "key"}), snapshot=snapshot
        )

    assert excinfo.value.context["permission"] == "secrets.read"
    assert SECRET not in str(excinfo.value)


async def test_policy_grant_permits_execution() -> None:
    snapshot, _ = build_snapshot(
        read_secret, policies={"read_secret": ToolPolicy(permissions=("secrets.read",))}
    )
    executor = ToolExecutor(policy=GrantPolicy(["secrets.read"]))

    result = await executor.execute(
        ToolRequest(name="read_secret", args={"name": "k"}), snapshot=snapshot
    )

    assert result.content == SECRET


async def test_approval_required_tool_fails_closed_without_a_gate() -> None:
    snapshot, _ = build_snapshot(
        add_numbers, policies={"add_numbers": ToolPolicy(approval_required=True)}
    )

    with pytest.raises(PolicyDenied) as excinfo:
        await ToolExecutor().execute(
            ToolRequest(name="add_numbers", args={"a": 1, "b": 1}), snapshot=snapshot
        )

    assert excinfo.value.context["reason"] == "approval"


async def test_approval_gate_can_approve() -> None:
    snapshot, _ = build_snapshot(
        add_numbers, policies={"add_numbers": ToolPolicy(approval_required=True)}
    )
    executor = ToolExecutor(approvals=AutoApprove())

    result = await executor.execute(
        ToolRequest(name="add_numbers", args={"a": 1, "b": 1}), snapshot=snapshot
    )

    assert result.content == 2


async def test_policy_can_require_approval_for_a_granted_permission() -> None:
    snapshot, _ = build_snapshot(
        add_numbers, policies={"add_numbers": ToolPolicy(permissions=("shell.execute",))}
    )
    executor = ToolExecutor(
        policy=GrantPolicy(["shell.execute"], approval_required=["shell.execute"])
    )

    with pytest.raises(PolicyDenied):
        await executor.execute(
            ToolRequest(name="add_numbers", args={"a": 1, "b": 1}), snapshot=snapshot
        )

    approved = ToolExecutor(
        policy=GrantPolicy(["shell.execute"], approval_required=["shell.execute"]),
        approvals=AutoApprove(),
    )
    assert (
        await approved.execute(
            ToolRequest(name="add_numbers", args={"a": 1, "b": 1}), snapshot=snapshot
        )
    ).ok


async def test_budget_exhaustion_prevents_execution() -> None:
    snapshot, _ = build_snapshot(add_numbers)
    budget = BudgetGovernor(BudgetLimits(tool_calls=1))
    executor = ToolExecutor()

    await executor.execute(
        ToolRequest(name="add_numbers", args={"a": 1, "b": 1}), snapshot=snapshot, budget=budget
    )

    with pytest.raises(BudgetExceeded):
        await executor.execute(
            ToolRequest(name="add_numbers", args={"a": 1, "b": 1}), snapshot=snapshot, budget=budget
        )

    assert budget.consumed(BudgetDimension.TOOL_CALLS) == 1


async def test_tool_failure_is_normalized_and_redacted() -> None:
    snapshot, _ = build_snapshot(explode)
    executor = ToolExecutor(redactor=SecretRedactor([SECRET]))

    result = await executor.execute(
        ToolRequest(name="explode", args={"reason": "x"}), snapshot=snapshot
    )

    assert result.status == "error"
    assert result.error is not None
    assert "RuntimeError" in result.error
    assert SECRET not in result.error
    assert "<redacted>" in result.error
    assert result.redacted is True


async def test_tool_failure_can_be_raised_instead() -> None:
    snapshot, _ = build_snapshot(explode)

    with pytest.raises(ToolExecutionError) as excinfo:
        await ToolExecutor().execute(
            ToolRequest(name="explode", args={"reason": "x"}),
            snapshot=snapshot,
            raise_on_error=True,
        )

    assert isinstance(excinfo.value.__cause__, RuntimeError)


async def test_timeout_is_reported_as_a_normalized_result() -> None:
    snapshot, _ = build_snapshot(slow_async)
    executor = ToolExecutor(default_timeout_seconds=0.01)

    result = await executor.execute(
        ToolRequest(name="slow_async", args={"seconds": 5}), snapshot=snapshot
    )

    assert result.status == "timeout"
    assert result.error is not None and "deadline" in result.error


async def test_unknown_tool_name_in_a_tool_call_is_not_executed() -> None:
    snapshot, _ = build_snapshot(add_numbers)
    executor = ToolExecutor()

    with pytest.raises(ToolNotFound):
        await executor.execute(ToolRequest(name="read_secret", args={}), snapshot=snapshot)

    # The harness never calls into a tool it does not know about.
    assert snapshot.get("read_secret") is None


async def test_before_tool_execute_hook_can_transform_arguments() -> None:
    hooks = HookRegistry()
    scope = Scope("hooks")

    async def bump(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        args = dict(payload["args"])
        args["b"] = args["b"] + 10
        return {"args": args}

    hooks.register(
        scope=scope, event=HookEvent.BEFORE_TOOL_EXECUTE, handler=bump, mode=HookMode.TRANSFORM
    )
    snapshot, _ = build_snapshot(add_numbers)
    executor = ToolExecutor(hooks=hooks)

    result = await executor.execute(
        ToolRequest(name="add_numbers", args={"a": 1, "b": 1}), snapshot=snapshot
    )

    assert result.content == 12


async def test_before_tool_execute_hook_can_refuse_execution() -> None:
    hooks = HookRegistry()
    scope = Scope("hooks")

    async def refuse(payload: Mapping[str, Any]) -> bool:
        return True

    hooks.register(
        scope=scope, event=HookEvent.BEFORE_TOOL_EXECUTE, handler=refuse, mode=HookMode.BAIL
    )
    snapshot, _ = build_snapshot(add_numbers)
    executor = ToolExecutor(hooks=hooks)

    with pytest.raises(PolicyDenied) as excinfo:
        await executor.execute(
            ToolRequest(name="add_numbers", args={"a": 1, "b": 1}), snapshot=snapshot
        )

    assert excinfo.value.context["reason"] == "hook"


async def test_after_tool_execute_and_error_hooks_observe_outcomes() -> None:
    hooks = HookRegistry()
    scope = Scope("hooks")
    observed: list[tuple[str, str]] = []

    async def after(payload: Mapping[str, Any]) -> None:
        observed.append(("after", str(payload["status"])))

    async def on_error(payload: Mapping[str, Any]) -> None:
        observed.append(("error", str(payload["tool"])))

    hooks.register(scope=scope, event=HookEvent.AFTER_TOOL_EXECUTE, handler=after)
    hooks.register(
        scope=scope,
        event=HookEvent.TOOL_ERROR,
        handler=on_error,
        error_policy=HookErrorPolicy.RECORD,
    )
    snapshot, _ = build_snapshot(add_numbers, explode)
    executor = ToolExecutor(hooks=hooks)

    await executor.execute(
        ToolRequest(name="add_numbers", args={"a": 1, "b": 2}), snapshot=snapshot
    )
    await executor.execute(ToolRequest(name="explode", args={"reason": "x"}), snapshot=snapshot)

    assert observed == [("after", "ok"), ("error", "explode")]


async def test_telemetry_records_spans_and_policy_decisions() -> None:
    telemetry = RecordingTelemetry()
    snapshot, _ = build_snapshot(
        read_secret, policies={"read_secret": ToolPolicy(permissions=("secrets.read",))}
    )
    executor = ToolExecutor(policy=GrantPolicy(["secrets.read"]), telemetry=telemetry)

    await executor.execute(ToolRequest(name="read_secret", args={"name": "k"}), snapshot=snapshot)

    assert telemetry.span_names() == ["tool.execute"]
    assert telemetry.spans[0].attributes["tool"] == "read_secret"
    assert "policy.decision" in telemetry.event_names()
    assert all(not record.errors for record in telemetry.spans)


async def test_telemetry_never_receives_secret_material() -> None:
    telemetry = RecordingTelemetry()
    snapshot, _ = build_snapshot(explode)
    executor = ToolExecutor(telemetry=telemetry, redactor=SecretRedactor([SECRET]))

    await executor.execute(ToolRequest(name="explode", args={"reason": "x"}), snapshot=snapshot)

    rendered = str([record.to_dict() for record in telemetry.spans])
    assert SECRET not in rendered
    assert "<redacted>" in rendered


async def test_scope_owned_registration_survives_until_the_scope_closes() -> None:
    registry = ToolRegistry()
    scope = Scope("tools")
    registry.register(scope=scope, tool=add_numbers)
    snapshot = registry.snapshot("gen_0001")

    result = await ToolExecutor().execute(
        ToolRequest(name="add_numbers", args={"a": 4, "b": 5}), snapshot=snapshot
    )
    assert result.content == 9

    await scope.aclose()

    # The snapshot a run holds stays usable: physical disposal happens only when
    # no generation can reach the plugin (invariant I6).
    assert registry.names() == ()
    assert [entry.name for entry in snapshot.entries] == ["add_numbers"]


async def test_permission_objects_are_parsed_from_policy_text() -> None:
    policy = ToolPolicy(permissions=("filesystem.write:/workspace/**",))

    assert policy.permission_objects == (Permission("filesystem.write", "/workspace/**"),)
