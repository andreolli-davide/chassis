"""Plugin lifecycle and health.

Lifecycle describes *ownership* state; health describes *operational quality*.
They are deliberately separate: a transient upstream outage degrades a plugin
without destroying its instance, and an unloaded plugin has no meaningful health.

``PENDING -> LOADING -> ACTIVE -> UNLOADING -> DISPOSED`` with ``LOADING ->
FAILED`` on setup error. ``FAILED`` instances keep their diagnostic error and can
be retried or replaced; their partial effects have already been reverted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from chassis.capabilities.registry import CapabilityRegistration
from chassis.core.errors import HarnessStateError
from chassis.core.scope import Scope
from chassis.plugins.base import Plugin, PluginContext
from chassis.plugins.manifest import PluginManifest

__all__ = ["PluginHealth", "PluginInstance", "PluginState"]


class PluginState(StrEnum):
    """Ownership state of a plugin instance."""

    PENDING = "pending"
    LOADING = "loading"
    ACTIVE = "active"
    UNLOADING = "unloading"
    DISPOSED = "disposed"
    FAILED = "failed"


class PluginHealth(StrEnum):
    """Operational quality of a plugin instance."""

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


_ALLOWED_TRANSITIONS: Mapping[PluginState, frozenset[PluginState]] = {
    PluginState.PENDING: frozenset({PluginState.LOADING, PluginState.DISPOSED}),
    PluginState.LOADING: frozenset({PluginState.ACTIVE, PluginState.FAILED}),
    PluginState.ACTIVE: frozenset({PluginState.UNLOADING, PluginState.FAILED}),
    PluginState.UNLOADING: frozenset({PluginState.DISPOSED}),
    PluginState.FAILED: frozenset({PluginState.LOADING, PluginState.DISPOSED}),
    PluginState.DISPOSED: frozenset(),
}


@dataclass(slots=True)
class PluginInstance:
    """One mounted plugin instance, owned by exactly one scope.

    An instance may be referenced by multiple runtime generations; ``generation_refs``
    records how many live generations can currently reach it, which is what gates
    physical disposal.
    """

    instance_id: str
    entry_id: str
    manifest: PluginManifest
    plugin: Plugin
    scope: Scope
    config: Mapping[str, Any] = field(default_factory=dict)
    resolved: Mapping[str, CapabilityRegistration] = field(default_factory=dict)
    context: PluginContext | None = None
    entry_revision: int = 1
    state: PluginState = PluginState.PENDING
    health: PluginHealth = PluginHealth.UNKNOWN
    error: BaseException | None = None
    generation_refs: int = 0

    @property
    def identity(self) -> str:
        return f"{self.manifest.identity}#{self.instance_id}"

    def transition(self, new_state: PluginState) -> None:
        """Move to ``new_state``, rejecting illegal lifecycle transitions."""

        if new_state == self.state:
            return
        allowed = _ALLOWED_TRANSITIONS[self.state]
        if new_state not in allowed:
            raise HarnessStateError(
                f"illegal plugin transition {self.state.value} -> {new_state.value}",
                plugin=self.manifest.name,
                instance_id=self.instance_id,
                current=self.state.value,
                requested=new_state.value,
            )
        self.state = new_state

    def to_dict(self) -> dict[str, Any]:
        """Diagnostic view. Plugin configuration values are never included."""

        return {
            "instance_id": self.instance_id,
            "entry_id": self.entry_id,
            "entry_revision": self.entry_revision,
            "plugin": self.manifest.name,
            "version": self.manifest.version,
            "state": self.state.value,
            "health": self.health.value,
            "scope_id": self.scope.id,
            "generation_refs": self.generation_refs,
            "config_keys": sorted(self.config),
            "resolved_capabilities": {
                name: registration.registration_id for name, registration in self.resolved.items()
            },
            "error": None if self.error is None else type(self.error).__name__,
        }
