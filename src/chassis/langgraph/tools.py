"""Route LangGraph tool calls through the Chassis execution boundary.

LangGraph's ``ToolNode`` is used as-is; Chassis supplies an async tool-call
wrapper, so policy, approval, budget, deadline, tracing, and normalization apply
without rebuilding the node or reimplementing tool dispatch.

The wrapper reads the per-run :class:`~chassis.runtime.HarnessRunContext` from
LangGraph's public runtime object, so a tool call executes against the generation
the run acquired rather than whatever composition is current.

Two failure modes are deliberately different:

* a refusal (policy denial, unknown tool) becomes an error ``ToolMessage`` so the
  model learns it cannot do that and the graph can continue;
* budget exhaustion propagates, because the run is over budget and the harness
  must stop it rather than let it continue spending.

Synchronous execution is refused rather than silently bypassing the boundary.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from chassis.core.errors import BudgetExceeded, ChassisError, HarnessStateError
from chassis.runtime import HarnessRunContext
from chassis.tools.executor import ToolRequest

__all__ = ["harness_tool_node"]

ToolContinuation = Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]]

_SYNC_REFUSAL = (
    "harness-mediated tools require async execution: invoke the graph with ainvoke/astream "
    "so the Chassis tool boundary (policy, approval, budget, tracing) cannot be bypassed"
)


def harness_tool_node(
    tools: Sequence[BaseTool],
    *,
    name: str = "tools",
    tags: Sequence[str] | None = None,
) -> ToolNode:
    """Build a ``ToolNode`` whose calls flow through the harness boundary."""

    return ToolNode(
        list(tools),
        name=name,
        tags=list(tags) if tags is not None else None,
        awrap_tool_call=_boundary_wrapper,
        wrap_tool_call=_refuse_sync_execution,
    )


def _refuse_sync_execution(
    request: ToolCallRequest,
    execute: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
) -> ToolMessage | Command[Any]:
    raise HarnessStateError(_SYNC_REFUSAL, tool=request.tool_call.get("name", "?"))


async def _boundary_wrapper(
    request: ToolCallRequest, execute: ToolContinuation
) -> ToolMessage | Command[Any]:
    run_context = getattr(request.runtime, "context", None)
    if not isinstance(run_context, HarnessRunContext):
        # No Chassis run context: this graph is being executed outside the
        # harness, so fall back to upstream behaviour rather than inventing one.
        return await execute(request)

    tool_call = request.tool_call
    name = str(tool_call.get("name", ""))
    args = tool_call.get("args", {})
    tool_call_id = tool_call.get("id")

    executor = run_context.executor
    snapshot = run_context.tools
    if executor is None or snapshot is None:
        return await execute(request)

    try:
        result = await executor.execute(
            ToolRequest(
                name=name,
                args=args,
                tool_call_id=tool_call_id,
                generation_id=run_context.generation_id,
                run_id=run_context.run_id,
                user_id=run_context.user_id,
                tenant_id=run_context.tenant_id,
            ),
            snapshot=snapshot,
            budget=run_context.budget,
            hook_snapshot=run_context.hooks,
        )
    except BudgetExceeded:
        raise
    except ChassisError as refusal:
        return _refusal_message(name, tool_call_id, refusal)

    if result.ok:
        return ToolMessage(
            content=_content(result.content),
            tool_call_id=tool_call_id or "",
            name=name,
            artifact=result.artifact,
            status="success",
        )
    return ToolMessage(
        content=result.error or f"tool {name!r} failed",
        tool_call_id=tool_call_id or "",
        name=name,
        status="error",
    )


def _refusal_message(name: str, tool_call_id: str | None, refusal: ChassisError) -> ToolMessage:
    return ToolMessage(
        content=f"{refusal.message}",
        tool_call_id=tool_call_id or "",
        name=name,
        status="error",
        additional_kwargs={"chassis_refusal": type(refusal).__name__},
    )


def _content(value: Any) -> Any:
    """Tool messages carry text or structured content; anything else is stringified."""

    if value is None or isinstance(value, (str, list, dict)):
        return value
    return str(value)
