"""Read-only diagnostics.

Diagnostics are generated from authoritative runtime state, never scraped from
logs: an operator should not have to reverse-engineer why a plugin is inactive.
The surface is deliberately small and always redacted -- plugin configuration
values are described by key, never by value, because configuration may carry
secrets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from chassis.plugins.lifecycle import PluginState

if TYPE_CHECKING:
    from chassis.harness import Harness

__all__ = ["Diagnostics"]


class Diagnostics:
    """Read-only views over the harness control plane."""

    def __init__(self, harness: Harness) -> None:
        self._harness = harness

    def plugins(self) -> list[dict[str, Any]]:
        """Desired plugins with their state, health, and resolution outcome.

        The resolution shown is a dry run of the desired state, so it explains
        what *would* happen, including immediately after ``install`` and before
        the next reconciliation.
        """

        plan = self._harness.plan()
        payload: list[dict[str, Any]] = []
        for entry in self._harness.plugin_registry.entries():
            instance = self._harness.plugin_registry.instance(entry.entry_id)
            planned = plan.plan_for(entry.entry_id)
            payload.append(
                {
                    "entry_id": entry.entry_id,
                    "plugin": entry.manifest.identity,
                    "provides": dict(sorted(entry.manifest.provides.items())),
                    "requires": dict(sorted(entry.manifest.requires.items())),
                    "optional": dict(sorted(entry.manifest.optional.items())),
                    "permissions": sorted(entry.manifest.permissions),
                    "state": None if instance is None else instance.state.value,
                    "health": None if instance is None else instance.health.value,
                    "instance_id": None if instance is None else instance.instance_id,
                    "scope_id": None if instance is None else instance.scope.id,
                    "generation_refs": 0 if instance is None else instance.generation_refs,
                    "eligible": None if planned is None else planned.eligible,
                    "order": None if planned is None else planned.order,
                    "requirements": (
                        [] if planned is None else [item.to_dict() for item in planned.requirements]
                    ),
                    "reasons": [] if planned is None else list(planned.reasons),
                }
            )
        return payload

    def capabilities(self) -> list[dict[str, Any]]:
        """Registered providers of capability contracts."""

        return [
            registration.to_dict()
            for registration in self._harness.capability_registry.registrations()
        ]

    def dependencies(self) -> dict[str, Any]:
        """Dependency edges, activation ordering, pending plugins, and cycles."""

        plan = self._harness.plan()
        return {
            "activation_order": list(plan.activation_order),
            "pending": list(plan.pending),
            "edges": [list(edge) for edge in plan.edges],
            "cycles": [list(cycle) for cycle in plan.cycles],
        }

    def explain(self, entry_id: str) -> str:
        """Why one plugin is active, pending, or excluded."""

        return self._harness.plan().explain(entry_id)

    def status(self) -> dict[str, Any]:
        """Harness-level summary."""

        instances = self._harness.plugin_registry.instances()
        by_state: dict[str, int] = {}
        for instance in instances:
            by_state[instance.state.value] = by_state.get(instance.state.value, 0) + 1
        plan = self._harness.plan()
        return {
            "harness": self._harness.name,
            "state": self._harness.state.value,
            "plugins": {
                "desired": len(self._harness.plugin_registry.entries()),
                "mounted": len(instances),
                "by_state": dict(sorted(by_state.items())),
            },
            "capabilities": len(self._harness.capability_registry),
            "composition": {
                "eligible": len(plan.activation_order),
                "pending": len(plan.pending),
                "cycles": len(plan.cycles),
            },
            "failures": [failure.to_dict() for failure in self._harness.last_cleanup_failures],
        }

    def plugin(self, entry_id: str) -> dict[str, Any] | None:
        """Diagnostics for a single plugin entry."""

        for payload in self.plugins():
            if payload["entry_id"] == entry_id:
                return payload
        return None

    def failed_plugins(self) -> list[dict[str, Any]]:
        """Mounted instances whose lifecycle state is ``FAILED``."""

        return [
            instance.to_dict()
            for instance in self._harness.plugin_registry.instances()
            if instance.state is PluginState.FAILED
        ]
