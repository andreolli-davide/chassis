"""The harness-controlled tool execution boundary.

Every harness-mediated tool call flows through here::

    agent -> tool request -> ToolExecutor -> policy -> approval -> budget/deadline
          -> tracing -> tool ainvoke -> normalized result

The tool itself is untouched: it remains the object the plugin registered, so
schemas, names, and behaviour stay upstream-compatible. Chassis core never imports
a tool library; the executor calls the structural ``ainvoke`` contract only.

Refusals by the harness (unknown tool, policy denial, budget exhaustion) raise
typed errors, because the caller must not confuse "the harness refused" with "the
tool ran and failed". A tool that ran and failed produces a normalized failure
result instead, so an agent loop can react to it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from chassis.budget.governor import BudgetGovernor
from chassis.budget.models import BudgetDimension
from chassis.core.errors import (
    BudgetExceeded,
    ChassisError,
    PolicyDenied,
    ReplayMismatch,
    ToolExecutionError,
)
from chassis.hooks.registry import HookSnapshot
from chassis.hooks.types import HookEvent, HookResult
from chassis.policy.engine import PolicyEngine, PolicyRequest
from chassis.replay.models import BoundaryKind, ReplayFallback
from chassis.replay.session import ReplaySession, boundary_key
from chassis.secrets.redaction import SecretRedactor
from chassis.telemetry.base import NoopTelemetry, Telemetry
from chassis.tools.registry import RegisteredTool, ToolSnapshot

if TYPE_CHECKING:
    from chassis.hooks.registry import HookRegistry

__all__ = [
    "ApprovalGate",
    "ApprovalRequest",
    "AutoApprove",
    "DenyApprovals",
    "ToolExecutionResult",
    "ToolExecutor",
    "ToolRequest",
    "ToolStatus",
]

ToolStatus = Literal["ok", "error", "timeout"]


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """A request to run a tool through the harness boundary."""

    name: str
    args: Mapping[str, Any] | str
    tool_call_id: str | None = None
    generation_id: str | None = None
    run_id: str | None = None
    user_id: str | None = None
    tenant_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.name,
            "tool_call_id": self.tool_call_id,
            "generation_id": self.generation_id,
            "run_id": self.run_id,
        }


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """Normalized outcome of one tool call.

    ``content``/``artifact``/``error`` are the semantic result — historical
    when replayed. ``duration_seconds``, ``tool_call_id``, ``generation_id``,
    ``run_id``, and ``status`` are current attribution, stamped fresh even when
    a recording answers.
    """

    name: str
    status: ToolStatus
    content: Any = None
    artifact: Any = None
    error: str | None = None
    duration_seconds: float = 0.0
    tool_call_id: str | None = None
    generation_id: str | None = None
    run_id: str | None = None
    redacted: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_payload(self) -> dict[str, Any]:
        """Complete, round-trippable representation used by record/replay.

        Distinct from :meth:`to_dict`, which is a diagnostic view that deliberately
        omits content and artifacts.
        """

        return {
            "name": self.name,
            "status": self.status,
            "content": self.content,
            "artifact": self.artifact,
            "error": self.error,
            "duration_seconds": self.duration_seconds,
            "tool_call_id": self.tool_call_id,
            "generation_id": self.generation_id,
            "run_id": self.run_id,
            "redacted": self.redacted,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ToolExecutionResult:
        """Rebuild a result recorded by :meth:`to_payload`."""

        return cls(
            name=str(payload.get("name", "")),
            status=payload.get("status", "ok"),  # type: ignore[arg-type]
            content=payload.get("content"),
            artifact=payload.get("artifact"),
            error=payload.get("error"),
            duration_seconds=float(payload.get("duration_seconds") or 0.0),
            tool_call_id=payload.get("tool_call_id"),
            generation_id=payload.get("generation_id"),
            run_id=payload.get("run_id"),
            redacted=bool(payload.get("redacted", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.name,
            "status": self.status,
            "error": self.error,
            "duration_seconds": self.duration_seconds,
            "tool_call_id": self.tool_call_id,
            "generation_id": self.generation_id,
        }


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """What an approval gate is asked to approve."""

    tool: str
    args: Mapping[str, Any] | str
    permissions: tuple[str, ...]
    side_effects: tuple[str, ...]
    generation_id: str | None = None
    run_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "permissions": list(self.permissions),
            "side_effects": list(self.side_effects),
            "generation_id": self.generation_id,
            "run_id": self.run_id,
        }


@runtime_checkable
class ApprovalGate(Protocol):
    """Decides whether a tool call that needs approval may proceed."""

    async def approve(self, request: ApprovalRequest) -> bool:
        """Return whether the call is approved."""

        ...


class AutoApprove:
    """Approval gate that approves everything. For tests and unattended runs."""

    async def approve(self, request: ApprovalRequest) -> bool:
        return True


class DenyApprovals:
    """Approval gate that approves nothing.

    The default: a policy that requires approval cannot be satisfied unless an
    approver is configured, so an approval requirement fails closed.
    """

    async def approve(self, request: ApprovalRequest) -> bool:
        return False


class ToolExecutor:
    """Runs tools with policy, approval, budget, deadline, and tracing.

    Args:
        policy: Policy engine consulted before execution. Defaults to no
            restrictions.
        approvals: Approval gate for calls that require approval. Defaults to
            denying.
        hooks: Hook registry used to dispatch tool boundary events.
        telemetry: Instrumentation sink. Defaults to recording nothing.
        redactor: Redactor applied to error text and traced attributes so secret
            material cannot leak through tool failures.
        default_timeout_seconds: Timeout applied when a tool declares none.
    """

    def __init__(
        self,
        *,
        policy: PolicyEngine | None = None,
        approvals: ApprovalGate | None = None,
        hooks: HookRegistry | None = None,
        telemetry: Telemetry | None = None,
        redactor: SecretRedactor | None = None,
        default_timeout_seconds: float | None = None,
        replay: ReplaySession | None = None,
    ) -> None:
        self._policy = policy
        self._approvals = approvals if approvals is not None else DenyApprovals()
        self._hooks = hooks
        self._telemetry = telemetry if telemetry is not None else NoopTelemetry()
        self._redactor = redactor if redactor is not None else SecretRedactor()
        self._default_timeout_seconds = default_timeout_seconds
        self._replay = replay

    # ------------------------------------------------------------------ config

    @property
    def policy(self) -> PolicyEngine | None:
        return self._policy

    @property
    def redactor(self) -> SecretRedactor:
        return self._redactor

    @property
    def replay(self) -> ReplaySession | None:
        return self._replay

    # --------------------------------------------------------------- execution

    async def execute(
        self,
        request: ToolRequest,
        *,
        snapshot: ToolSnapshot,
        budget: BudgetGovernor | None = None,
        hook_snapshot: HookSnapshot | None = None,
        policy: PolicyEngine | None = None,
        raise_on_error: bool = False,
    ) -> ToolExecutionResult:
        """Execute one tool call.

        Args:
            request: Tool name, arguments, and correlation identifiers.
            snapshot: Tools reachable from the run's generation.
            budget: Governor for the run, if the run is budgeted.
            hook_snapshot: Hooks reachable from the run's generation.
            policy: Policy engine of the run's generation, overriding the
                executor default. A generation-provided policy is what makes a
                runtime-bound policy change take effect without rebuilding graphs.
            raise_on_error: Raise instead of returning a normalized failure.

        Raises:
            ToolNotFound: the tool is not in ``snapshot``.
            PolicyDenied: policy refused the call or approval was not granted.
            BudgetExceeded: the run has exhausted a budget dimension.
            ToolExecutionError: the tool failed and ``raise_on_error`` is set.
        """

        entry = snapshot.require(request.name)

        transformed = await self._dispatch(
            HookEvent.BEFORE_TOOL_EXECUTE,
            {
                "tool": entry.name,
                "owner": entry.owner_name,
                "args": request.args,
                "generation_id": request.generation_id or snapshot.generation_id,
                "run_id": request.run_id,
            },
            hook_snapshot,
        )
        if transformed.stopped:
            raise PolicyDenied(
                f"tool {entry.name!r} was refused by a hook",
                tool=entry.name,
                reason="hook",
            )
        args = transformed.payload.get("args", request.args)

        await self._authorize(
            entry,
            request,
            args,
            policy if policy is not None else self._policy,
            hook_snapshot,
        )
        try:
            self._charge(budget, entry)
        except BudgetExceeded as error:
            self._telemetry.event(
                "budget.exhausted",
                {"tool": entry.name, "dimension": error.context.get("dimension")},
            )
            raise

        # Authorization and budgeting run even when the call is replayed: a
        # recording answers *what the tool returned*, never whether the harness
        # was allowed to ask. Replay reuses only the recorded semantic result:
        # run, generation, call id, and timing attribution are stamped fresh,
        # and the live boundary — span and hooks — is the same either way.
        replay_key = boundary_key(BoundaryKind.TOOL.value, entry.name, args)
        replayed: Any = None
        if self._replay is not None and self._replay.is_replaying:
            if self._replay.has_remaining(BoundaryKind.TOOL, key=replay_key):
                replayed = self._replay.replay(BoundaryKind.TOOL, key=replay_key).response
            elif self._replay.fallback is ReplayFallback.ERROR:
                raise ReplayMismatch(
                    "tool call was not recorded and replay does not fall back to live execution",
                    tool=entry.name,
                    recorded=self._replay.counts().get(BoundaryKind.TOOL.value, 0),
                )

        started = time.monotonic()
        async with self._telemetry.span(
            "tool.execute",
            {
                "tool": entry.name,
                "owner": entry.owner_name,
                "generation_id": request.generation_id or snapshot.generation_id,
                "idempotent": entry.policy.idempotent,
                "replayed": replayed is not None,
            },
        ) as span:
            if replayed is not None:
                prior = ToolExecutionResult.from_payload(replayed)
                result = ToolExecutionResult(
                    name=entry.name,
                    status=prior.status,
                    content=prior.content,
                    artifact=prior.artifact,
                    error=prior.error,
                    duration_seconds=time.monotonic() - started,
                    tool_call_id=request.tool_call_id,
                    generation_id=request.generation_id or snapshot.generation_id,
                    run_id=request.run_id,
                    redacted=prior.redacted,
                )
                if result.status != "ok":
                    # A recorded failure follows the live failure shape: the
                    # error hook fires and the after hook does not.
                    message = result.error or ""
                    span.record_error(ToolExecutionError(message, tool=entry.name))
                    await self._dispatch(
                        HookEvent.TOOL_ERROR,
                        {"tool": entry.name, "error": message, "run_id": request.run_id},
                        hook_snapshot,
                    )
                    return result
            else:
                try:
                    content, artifact = await self._invoke(entry, args, request)
                except (TimeoutError, asyncio.CancelledError) as error:
                    if isinstance(error, asyncio.CancelledError):
                        span.record_error(error)
                        raise
                    duration = time.monotonic() - started
                    message, was_redacted = self._redactor.redact_and_report(
                        f"tool {entry.name!r} exceeded its {self._timeout_for(entry)}s deadline"
                    )
                    span.record_error(TimeoutError(message))
                    await self._dispatch(
                        HookEvent.TOOL_ERROR,
                        {"tool": entry.name, "error": message, "run_id": request.run_id},
                        hook_snapshot,
                    )
                    result = ToolExecutionResult(
                        name=entry.name,
                        status="timeout",
                        error=message,
                        duration_seconds=duration,
                        tool_call_id=request.tool_call_id,
                        generation_id=request.generation_id or snapshot.generation_id,
                        run_id=request.run_id,
                        redacted=was_redacted,
                    )
                    if raise_on_error:
                        raise ToolExecutionError(
                            message, tool=entry.name, status="timeout"
                        ) from error
                    return self._recorded(result, replay_key, entry, args, request)
                except Exception as error:
                    duration = time.monotonic() - started
                    message, was_redacted = self._normalize_error(entry, error)
                    # The span receives the redacted form: telemetry must never
                    # see secret material, including through error text.
                    span.record_error(ToolExecutionError(message, tool=entry.name))
                    await self._dispatch(
                        HookEvent.TOOL_ERROR,
                        {"tool": entry.name, "error": message, "run_id": request.run_id},
                        hook_snapshot,
                    )
                    if raise_on_error:
                        raise ToolExecutionError(message, tool=entry.name) from error
                    return self._recorded(
                        ToolExecutionResult(
                            name=entry.name,
                            status="error",
                            error=message,
                            duration_seconds=duration,
                            tool_call_id=request.tool_call_id,
                            generation_id=request.generation_id or snapshot.generation_id,
                            run_id=request.run_id,
                            redacted=was_redacted,
                        ),
                        replay_key,
                        entry,
                        args,
                        request,
                    )

                duration = time.monotonic() - started
                result = self._recorded(
                    ToolExecutionResult(
                        name=entry.name,
                        status="ok",
                        content=content,
                        artifact=artifact,
                        duration_seconds=duration,
                        tool_call_id=request.tool_call_id,
                        generation_id=request.generation_id or snapshot.generation_id,
                        run_id=request.run_id,
                    ),
                    replay_key,
                    entry,
                    args,
                    request,
                )
        await self._dispatch(
            HookEvent.AFTER_TOOL_EXECUTE,
            {
                "tool": entry.name,
                "status": result.status,
                "duration_seconds": result.duration_seconds,
                "run_id": request.run_id,
            },
            hook_snapshot,
        )
        return result

    # -------------------------------------------------------------- internals

    def _recorded(
        self,
        result: ToolExecutionResult,
        replay_key: str,
        entry: RegisteredTool,
        args: Mapping[str, Any] | str,
        request: ToolRequest,
    ) -> ToolExecutionResult:
        """Record a completed call when the session is recording."""

        if self._replay is not None:
            self._replay.record(
                BoundaryKind.TOOL,
                key=replay_key,
                request={"tool": entry.name, "args": args},
                response=result.to_payload(),
                generation_id=request.generation_id,
                run_id=request.run_id,
            )
        return result

    def _timeout_for(self, entry: RegisteredTool) -> float | None:
        return (
            entry.policy.timeout_seconds
            if entry.policy.timeout_seconds is not None
            else self._default_timeout_seconds
        )

    async def _authorize(
        self,
        entry: RegisteredTool,
        request: ToolRequest,
        args: Mapping[str, Any] | str,
        engine: PolicyEngine | None,
        hooks: HookSnapshot | None,
    ) -> None:
        policy = entry.policy
        approval_required = policy.approval_required

        if engine is not None:
            for permission in policy.permission_objects:
                try:
                    result = await engine.evaluate(
                        PolicyRequest(
                            permission=permission,
                            subject=f"tool:{entry.name}",
                            resource=permission.resource,
                            context={
                                "owner": entry.owner_name,
                                "generation_id": request.generation_id,
                            },
                        )
                    )
                except Exception as error:
                    # A policy provider that cannot decide denies: "could not
                    # decide" must never be read as "allowed". The original
                    # exception stays an internal cause; the public error and the
                    # observed reason carry no provider text.
                    await self._dispatch(
                        HookEvent.POLICY_DECISION,
                        {
                            "tool": entry.name,
                            "owner": entry.owner_name,
                            "permission": str(permission),
                            "allowed": False,
                            "reason": "policy provider failure",
                            "generation_id": request.generation_id,
                            "run_id": request.run_id,
                        },
                        hooks,
                    )
                    self._telemetry.event(
                        "policy.decision",
                        {
                            "permission": str(permission),
                            "tool": entry.name,
                            "allowed": False,
                            "reason": "policy provider failure",
                        },
                    )
                    raise PolicyDenied(
                        f"tool {entry.name!r} was denied: the policy provider failed",
                        tool=entry.name,
                        permission=str(permission),
                        reason="policy provider failure",
                    ) from error
                await self._dispatch(
                    HookEvent.POLICY_DECISION,
                    {
                        "tool": entry.name,
                        "owner": entry.owner_name,
                        "permission": str(permission),
                        "allowed": result.allowed,
                        "reason": self._redactor.redact(result.reason),
                        "generation_id": request.generation_id,
                        "run_id": request.run_id,
                    },
                    hooks,
                )
                self._telemetry.event(
                    "policy.decision",
                    {
                        "permission": str(permission),
                        "tool": entry.name,
                        "allowed": result.allowed,
                        "reason": self._redactor.redact(result.reason),
                    },
                )
                if not result.allowed:
                    reason = self._redactor.redact(result.reason)
                    raise PolicyDenied(
                        f"tool {entry.name!r} requires {permission}: {reason}",
                        tool=entry.name,
                        permission=str(permission),
                        reason=reason,
                    )
                approval_required = approval_required or result.approval_required
            await self._dispatch(
                HookEvent.POLICY_DECISION,
                {
                    "tool": entry.name,
                    "owner": entry.owner_name,
                    "allowed": True,
                    "permissions": list(policy.permissions),
                    "generation_id": request.generation_id,
                    "run_id": request.run_id,
                },
                hooks,
            )
            self._telemetry.event(
                "policy.decision",
                {"tool": entry.name, "allowed": True, "permissions": list(policy.permissions)},
            )

        if approval_required:
            approved = await self._approvals.approve(
                ApprovalRequest(
                    tool=entry.name,
                    args=args,
                    permissions=policy.permissions,
                    side_effects=policy.side_effects,
                    generation_id=request.generation_id,
                    run_id=request.run_id,
                    metadata=request.metadata,
                )
            )
            if not approved:
                await self._dispatch(
                    HookEvent.POLICY_DECISION,
                    {
                        "tool": entry.name,
                        "owner": entry.owner_name,
                        "allowed": False,
                        "reason": "approval",
                        "generation_id": request.generation_id,
                        "run_id": request.run_id,
                    },
                    hooks,
                )
                raise PolicyDenied(
                    f"tool {entry.name!r} requires approval that was not granted",
                    tool=entry.name,
                    reason="approval",
                    permissions=list(policy.permissions),
                )

    def _charge(self, budget: BudgetGovernor | None, entry: RegisteredTool) -> None:
        if budget is None:
            return
        budget.consume(BudgetDimension.TOOL_CALLS)
        budget.check(BudgetDimension.WALL_CLOCK_SECONDS)

    async def _invoke(
        self,
        entry: RegisteredTool,
        args: Mapping[str, Any] | str,
        request: ToolRequest,
    ) -> tuple[Any, Any]:
        config = self._runnable_config(entry, request)
        payload: str | dict[str, Any] = args if isinstance(args, str) else dict(args)
        timeout = self._timeout_for(entry)
        if timeout is None:
            output = await entry.tool.ainvoke(payload, config=config)
        else:
            async with asyncio.timeout(timeout):
                output = await entry.tool.ainvoke(payload, config=config)
        return _split_output(output)

    def _runnable_config(self, entry: RegisteredTool, request: ToolRequest) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "chassis_tool": entry.name,
            "chassis_tool_owner": entry.owner_name,
            "chassis_cost_class": entry.policy.cost_class,
            "chassis_generation_id": request.generation_id,
            "chassis_run_id": request.run_id,
        }
        config: dict[str, Any] = {
            "tags": [f"chassis:tool:{entry.name}"],
            "metadata": self._redactor.redact_value({k: v for k, v in metadata.items() if v}),
        }
        if request.tool_call_id:
            config["metadata"]["chassis_tool_call_id"] = request.tool_call_id
        return config

    def _normalize_error(self, entry: RegisteredTool, error: BaseException) -> tuple[str, bool]:
        """Normalize and redact a tool failure, reporting whether redaction applied."""

        if isinstance(error, ChassisError):
            base = f"{type(error).__name__}: {error.message}"
        else:
            base = f"{type(error).__name__}: {error}"
        return self._redactor.redact_and_report(f"tool {entry.name!r} failed: {base}")

    async def _dispatch(
        self,
        event: HookEvent,
        payload: Mapping[str, Any],
        hooks: HookSnapshot | None,
    ) -> HookResult:
        if self._hooks is None:
            return HookResult(event=event, payload=payload)
        return await self._hooks.dispatch(event, payload, hooks=hooks)


def _split_output(output: Any) -> tuple[Any, Any]:
    """Normalize a tool return value into ``(content, artifact)``."""

    if isinstance(output, tuple) and len(output) == 2:
        content, artifact = output
        return content, artifact
    artifact = getattr(output, "artifact", None)
    content = getattr(output, "content", output)
    return content, artifact
