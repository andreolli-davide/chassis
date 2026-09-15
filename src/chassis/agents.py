"""Agent registration and invocation.

The registry is backend-agnostic: it stores :class:`~chassis.runtime.AgentRuntime`
implementations and, for each invocation, acquires the current runtime generation,
assembles the generation's environment, and hands an immutable run context to the
execution engine.

A run therefore observes exactly the composition it acquired. Invocation never
takes the control-plane lock: acquisition is a lease on an immutable generation,
and execution happens without it (invariant I8).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any

from chassis.budget.governor import BudgetGovernor
from chassis.budget.models import BudgetLimits
from chassis.core.errors import ChassisError, ConfigurationError, HarnessStateError
from chassis.core.scope import Scope
from chassis.runtime import (
    AgentEvent,
    AgentRequest,
    AgentResult,
    AgentRuntime,
    HarnessRunContext,
)

if TYPE_CHECKING:
    from chassis.harness import Harness

__all__ = ["AgentNotFound", "AgentRegistry", "ScopedAgents"]


class AgentNotFound(ChassisError):
    """Raised when an agent name is not registered."""

    code = "agent_not_found"


class AgentRegistry:
    """Registered execution engines, plus the app-facing invocation surface.

    Args:
        harness: Owning harness, used to acquire generations and assemble the run
            environment. ``None`` disables invocation (registration only), which is
            useful for isolated unit tests of an agent runtime.
    """

    def __init__(self, *, harness: Harness | None = None) -> None:
        self._harness = harness
        self._runtimes: dict[str, AgentRuntime] = {}

    # ------------------------------------------------------------- registration

    def register(
        self,
        runtime: AgentRuntime,
        *,
        scope: Scope | None = None,
        replace: bool = False,
    ) -> AgentRuntime:
        """Register an agent runtime under its name.

        When ``scope`` is given the registration is owned by it, so unloading the
        plugin that provided the agent also removes the agent.
        """

        name = getattr(runtime, "name", "")
        if not name:
            raise ConfigurationError(
                "agent runtime must declare a name", runtime=type(runtime).__name__
            )
        if scope is not None:
            scope.assert_open(f"register agent {name!r}")
        if name in self._runtimes and not replace:
            raise ConfigurationError("agent name is already registered", agent=name)
        self._runtimes[name] = runtime
        if scope is not None:
            scope.cleanup(f"agent {name}", self.unregister, name, kind="agent")
        return runtime

    def unregister(self, name: str) -> bool:
        """Remove an agent by name. Returns whether it was present."""

        return self._runtimes.pop(name, None) is not None

    def get(self, name: str) -> AgentRuntime:
        """Return a registered runtime.

        Raises:
            AgentNotFound: the name is not registered.
        """

        runtime = self._runtimes.get(name)
        if runtime is None:
            raise AgentNotFound(
                f"agent {name!r} is not registered", agent=name, available=list(self.names())
            )
        return runtime

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._runtimes))

    def __len__(self) -> int:
        return len(self._runtimes)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._runtimes

    def to_dict(self) -> dict[str, Any]:
        return {"agents": list(self.names())}

    # -------------------------------------------------------------- invocation

    async def invoke(
        self,
        agent: str,
        input: Any = None,
        *,
        request: AgentRequest | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
        thread_id: str | None = None,
        resume: Any = None,
        checkpoint_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        limits: BudgetLimits | None = None,
    ) -> AgentResult:
        """Execute ``agent`` once and return its result.

        Either ``input`` or a fully formed ``request`` may be supplied, not both.
        """

        harness = self._require_harness()
        runtime = self.get(agent)
        agent_request = self._build_request(
            request,
            input=input,
            thread_id=thread_id,
            resume=resume,
            checkpoint_id=checkpoint_id,
            metadata=metadata,
        )

        await harness.ensure_ready()
        async with harness.acquire() as generation:
            environment = harness.run_environment(generation, limits=limits)
            run_context = HarnessRunContext.new(
                generation=generation,
                environment=environment,
                agent=agent,
                user_id=user_id,
                tenant_id=tenant_id,
                thread_id=agent_request.thread_id,
                metadata=agent_request.metadata,
            )
            graph_digest = _definition_digest(runtime, run_context)
            snapshot = harness.snapshot_for(
                generation, agent=agent, graph_definition_hash=graph_digest
            )
            started = time.monotonic()
            async with harness.telemetry.span(
                "agent.run",
                {
                    "agent": agent,
                    "generation_id": generation.generation_id,
                    "thread_id": agent_request.thread_id,
                    "chassis_version": snapshot.chassis_version,
                    "snapshot_digest": snapshot.digest(),
                    "plugin_graph_hash": snapshot.plugin_graph_hash,
                    "graph_definition_hash": graph_digest,
                    "tool_schema_hash": snapshot.tool_schema_hash,
                },
            ):
                result = await runtime.invoke(agent_request, run_context)
            return _with_duration(
                _with_snapshot(result, snapshot.digest()), time.monotonic() - started
            )

    async def stream(
        self,
        agent: str,
        input: Any = None,
        *,
        request: AgentRequest | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
        thread_id: str | None = None,
        resume: Any = None,
        checkpoint_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        limits: BudgetLimits | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Execute ``agent`` and yield events as they occur.

        The generation is leased for as long as the stream is consumed.
        """

        harness = self._require_harness()
        runtime = self.get(agent)
        agent_request = self._build_request(
            request,
            input=input,
            thread_id=thread_id,
            resume=resume,
            checkpoint_id=checkpoint_id,
            metadata=metadata,
        )

        await harness.ensure_ready()
        async with harness.acquire() as generation:
            environment = harness.run_environment(generation, limits=limits)
            run_context = HarnessRunContext.new(
                generation=generation,
                environment=environment,
                agent=agent,
                user_id=user_id,
                tenant_id=tenant_id,
                thread_id=agent_request.thread_id,
                metadata=agent_request.metadata,
            )
            async for event in runtime.stream(agent_request, run_context):
                yield event

    # -------------------------------------------------------------- internals

    def _require_harness(self) -> Harness:
        if self._harness is None:
            raise HarnessStateError("agent registry is not bound to a harness")
        return self._harness

    @staticmethod
    def _build_request(
        request: AgentRequest | None,
        *,
        input: Any,
        thread_id: str | None,
        resume: Any,
        checkpoint_id: str | None,
        metadata: Mapping[str, Any] | None,
    ) -> AgentRequest:
        if request is not None:
            if input is not None:
                raise ConfigurationError("pass either input or request, not both")
            return request
        return AgentRequest(
            input=input,
            thread_id=thread_id,
            resume=resume,
            checkpoint_id=checkpoint_id,
            metadata=dict(metadata or {}),
        )


class ScopedAgents:
    """Plugin-facing facade bound to one plugin instance scope."""

    __slots__ = ("_registry", "_scope")

    def __init__(self, registry: AgentRegistry, scope: Scope) -> None:
        self._registry = registry
        self._scope = scope

    def register(self, runtime: AgentRuntime, *, replace: bool = False) -> AgentRuntime:
        """Register an agent for the lifetime of the plugin scope."""

        return self._registry.register(runtime, scope=self._scope, replace=replace)

    def unregister(self, name: str) -> bool:
        """Remove an agent early, before the scope closes."""

        return self._registry.unregister(name)


def _definition_digest(runtime: AgentRuntime, run_context: HarnessRunContext) -> str | None:
    """Ask a runtime for its graph-definition digest, when it has one.

    Optional on purpose: a backend without compiled graphs simply contributes
    nothing to the snapshot.
    """

    digest = getattr(runtime, "definition_digest", None)
    if not callable(digest):
        return None
    value = digest(run_context)
    return value if isinstance(value, str) else None


def _with_snapshot(result: AgentResult, digest: str) -> AgentResult:
    from dataclasses import replace

    return replace(result, metadata={**dict(result.metadata), "snapshot_digest": digest})


def _with_duration(result: AgentResult, duration: float) -> AgentResult:
    if result.duration_seconds:
        return result
    from dataclasses import replace

    return replace(result, duration_seconds=duration)


def environment_budget(limits: BudgetLimits | None) -> BudgetGovernor | None:
    """Build the budget governor for one run, if any limits apply."""

    if limits is None or limits.is_unlimited:
        return None
    return BudgetGovernor(limits)
