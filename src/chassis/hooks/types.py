"""Hook events, modes, and results.

Hooks cover boundaries that *Chassis* owns -- lifecycle, generation publication,
policy decisions, tool execution. They deliberately do not duplicate the
LangGraph/LangChain callback system for anything happening inside a graph.

The initial surface supports three semantics: observe, transform, and bail.
``around`` handlers are omitted on purpose: graph-internal middleware is where
wrapping belongs, and a hand-rolled continuation protocol here would duplicate
what LangGraph already provides.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from chassis.core.errors import CleanupFailure

__all__ = [
    "HookErrorPolicy",
    "HookEvent",
    "HookHandler",
    "HookMode",
    "HookPayload",
    "HookRegistration",
    "HookResult",
]

#: Payload passed to hook handlers. Immutable by construction.
HookPayload = Mapping[str, Any]

#: A hook handler: an async callable receiving the payload.
HookHandler = Callable[[HookPayload], Awaitable["Mapping[str, Any] | bool | None"]]


class HookEvent(StrEnum):
    """Boundaries at which Chassis dispatches hooks.

    Control-plane events (``PLUGIN_*``, ``GENERATION_*``) are dispatched against the
    live registry while a composition is being built or taken apart; their handler
    failures are aggregated like cleanup failures rather than aborting the
    transition. Data-plane events (``*_TOOL_EXECUTE``, ``TOOL_ERROR``,
    ``BEFORE_AGENT_RUN``, ``AFTER_AGENT_RUN``, ``AGENT_ERROR``,
    ``POLICY_DECISION``) are dispatched against the run's generation snapshot.

    Only the entry boundaries can refuse: a bail at ``BEFORE_TOOL_EXECUTE`` or
    ``BEFORE_AGENT_RUN`` stops the operation and is reported as ``PolicyDenied``.
    Everywhere else a bail simply ends the handler chain.
    """

    PLUGIN_MOUNTING = "plugin_mounting"
    PLUGIN_MOUNTED = "plugin_mounted"
    PLUGIN_UNMOUNTING = "plugin_unmounting"
    PLUGIN_UNMOUNTED = "plugin_unmounted"
    BEFORE_AGENT_RUN = "before_agent_run"
    AFTER_AGENT_RUN = "after_agent_run"
    AGENT_ERROR = "agent_error"
    BEFORE_TOOL_EXECUTE = "before_tool_execute"
    AFTER_TOOL_EXECUTE = "after_tool_execute"
    TOOL_ERROR = "tool_error"
    GENERATION_PUBLISHED = "generation_published"
    GENERATION_DRAINING = "generation_draining"
    POLICY_DECISION = "policy_decision"


class HookMode(StrEnum):
    """What the harness does with a handler's return value.

    ``OBSERVE``
        The return value is ignored. Use for logging, metrics, and audit.
    ``TRANSFORM``
        A returned mapping replaces the payload for the remaining handlers and for
        the caller. Returning ``None`` leaves the payload unchanged.
    ``BAIL``
        A truthy return value stops the chain and is reported to the caller.
    """

    OBSERVE = "observe"
    TRANSFORM = "transform"
    BAIL = "bail"


class HookErrorPolicy(StrEnum):
    """How a failing handler affects the dispatch."""

    RECORD = "record"
    RAISE = "raise"


@dataclass(frozen=True, slots=True, eq=False)
class HookRegistration:
    """One registered handler, owned by a scope."""

    registration_id: str
    event: HookEvent
    handler: HookHandler
    handler_name: str
    mode: HookMode
    priority: int
    error_policy: HookErrorPolicy
    sequence: int
    owner_id: str
    scope_id: str

    @property
    def order_key(self) -> tuple[int, int]:
        """Deterministic ordering: priority first, registration order second."""

        return (self.priority, self.sequence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "registration_id": self.registration_id,
            "event": self.event.value,
            "handler": self.handler_name,
            "mode": self.mode.value,
            "priority": self.priority,
            "error_policy": self.error_policy.value,
            "owner_id": self.owner_id,
            "scope_id": self.scope_id,
        }


@dataclass(frozen=True, slots=True)
class HookResult:
    """Outcome of dispatching one event."""

    event: HookEvent
    payload: HookPayload
    stopped: bool = False
    failures: tuple[CleanupFailure, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.failures
