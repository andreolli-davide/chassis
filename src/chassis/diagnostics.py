"""Read-only diagnostics.

Diagnostics are generated from authoritative runtime state, never scraped from
logs: an operator should not have to reverse-engineer why a plugin is inactive.
The surface is deliberately small and always redacted -- plugin configuration
values are described by key, never by value, because configuration may carry
secrets.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from chassis.harness import Harness
    from chassis.plugins.lifecycle import PluginInstance

__all__ = [
    "Diagnostics",
    "GenerationPressureEntry",
    "GenerationPressureReport",
]


@dataclass(frozen=True, slots=True)
class GenerationPressureEntry:
    """Authoritative state of one live generation."""

    generation_id: str
    sequence: int
    state: str
    is_current: bool
    age_seconds: float
    leases: int
    oldest_lease_age_seconds: float | None
    retained_plugins: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "sequence": self.sequence,
            "state": self.state,
            "is_current": self.is_current,
            "age_seconds": self.age_seconds,
            "leases": self.leases,
            "oldest_lease_age_seconds": self.oldest_lease_age_seconds,
            "retained_plugins": [dict(plugin) for plugin in self.retained_plugins],
        }


@dataclass(frozen=True, slots=True)
class GenerationPressureReport:
    """How many generations are alive, why, and what they still retain.

    Every value is read from authoritative runtime state: the generation manager's
    live set, each generation's own lease accounting, and the plugin instances the
    generation was published with. Nothing here is inferred from logs or the
    bounded diagnostics history, and nothing here changes runtime behaviour -- this
    is observability, not enforcement.

    A generation stays live while a run holds a lease on it. That is correct: the
    run must keep observing the composition it acquired. Pressure tells an operator
    when that is happening for longer than expected.
    """

    current_generation_id: str | None
    live_generations: int
    draining_generations: int
    total_leases: int
    oldest_lease_age_seconds: float | None
    generations: tuple[GenerationPressureEntry, ...]
    instance_generations: Mapping[str, tuple[str, ...]]
    history_limit: int
    history_retained: int
    history_evicted: int
    generated_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.instance_generations, MappingProxyType):
            object.__setattr__(
                self, "instance_generations", MappingProxyType(dict(self.instance_generations))
            )

    def to_dict(self) -> dict[str, Any]:
        """Structured, JSON-compatible form. Never contains configuration values."""

        return {
            "current_generation_id": self.current_generation_id,
            "live_generations": self.live_generations,
            "draining_generations": self.draining_generations,
            "total_leases": self.total_leases,
            "oldest_lease_age_seconds": self.oldest_lease_age_seconds,
            "generations": [generation.to_dict() for generation in self.generations],
            "instance_generations": {
                instance_id: list(ids)
                for instance_id, ids in sorted(self.instance_generations.items())
            },
            "history": {
                "limit": self.history_limit,
                "retained": self.history_retained,
                "evicted": self.history_evicted,
            },
            "generated_at": self.generated_at,
        }

    def metrics(self) -> dict[str, float]:
        """Vendor-neutral gauges a telemetry backend may export.

        Names are Chassis-owned and dotted, so a backend can forward them without
        the core depending on any metrics library.
        """

        return {
            "chassis.generations.live": float(self.live_generations),
            "chassis.generations.draining": float(self.draining_generations),
            "chassis.generations.leases": float(self.total_leases),
            "chassis.generations.oldest_lease_age_seconds": float(
                self.oldest_lease_age_seconds or 0.0
            ),
        }

    def to_text(self) -> str:
        """Human-readable rendering of the report."""

        oldest = self.oldest_lease_age_seconds
        lines = [
            f"current_generation: {self.current_generation_id or '(none)'}",
            f"live_generations: {self.live_generations}",
            f"draining_generations: {self.draining_generations}",
            f"oldest_lease_age_seconds: {'-' if oldest is None else f'{oldest:.0f}'}",
        ]
        for generation in self.generations:
            lines.append("")
            label = " (current)" if generation.is_current else ""
            lines.append(f"{generation.generation_id}{label}")
            lines.append(f"  state: {generation.state}")
            lines.append(f"  age_seconds: {generation.age_seconds:.0f}")
            lines.append(f"  leases: {generation.leases}")
            if generation.oldest_lease_age_seconds is not None:
                lines.append(
                    f"  oldest_lease_age_seconds: {generation.oldest_lease_age_seconds:.0f}"
                )
            lines.append("  retained_plugins:")
            if not generation.retained_plugins:
                lines.append("    (none)")
            for plugin in generation.retained_plugins:
                lines.append(f"    - {plugin['entry_id']} ({plugin['plugin']})")
        if self.history_evicted:
            lines.append("")
            lines.append(
                f"history: {self.history_retained}/{self.history_limit} retained, "
                f"{self.history_evicted} evicted"
            )
        return "\n".join(lines)


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
                    "effects": [] if instance is None else self._effects(instance),
                    "eligible": None if planned is None else planned.eligible,
                    "order": None if planned is None else planned.order,
                    "requirements": (
                        [] if planned is None else [item.to_dict() for item in planned.requirements]
                    ),
                    "reasons": [] if planned is None else list(planned.reasons),
                }
            )
        return payload

    def _effects(self, instance: PluginInstance) -> list[dict[str, Any]]:
        """Owned effects of one instance, with descriptions redacted.

        Effect descriptions are written by plugin authors, so they are redacted
        before they leave the harness: diagnostics never become a place where a
        secret accumulates.
        """

        redactor = self._harness.redactor
        return [
            {
                "effect_id": effect.effect_id,
                "kind": effect.kind,
                "description": redactor.redact(effect.description),
                "scope_id": effect.scope_id,
            }
            for effect in instance.scope.effects
        ]

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

    def config(self) -> dict[str, Any] | None:
        """The declarative configuration applied most recently, if any."""

        config = self._harness.config
        return None if config is None else config.to_dict()

    def desired_state(self) -> list[dict[str, Any]]:
        """What reconciliation would change, relative to the applied configuration.

        Empty when no declarative configuration has been applied, because there is
        nothing to compare programmatic installs against.
        """

        from chassis.config.reconcile import InstalledEntry, config_fingerprint, diff_desired_state

        config = self._harness.config
        if config is None:
            return []
        installed = {
            entry.entry_id: InstalledEntry(
                entry_id=entry.entry_id,
                plugin=entry.manifest.name,
                revision=entry.revision,
                config_fingerprint=config_fingerprint(
                    plugin=entry.manifest.name, config=entry.config
                ),
            )
            for entry in self._harness.plugin_registry.entries()
        }
        return [change.to_dict() for change in diff_desired_state(config, installed)]

    def agents(self) -> dict[str, Any]:
        """Registered agent runtimes."""

        return self._harness.agents.to_dict()

    def tools(self) -> dict[str, Any]:
        """Registered tools with their owner and policy."""

        return self._harness.tools.to_dict()

    def hooks(self) -> dict[str, Any]:
        """Registered hooks with their owner, mode, and ordering."""

        return self._harness.hooks.to_dict()

    def generations(self) -> list[dict[str, Any]]:
        """Published generations, newest first, with lease counts and state."""

        return [
            generation.to_dict()
            for generation in self._harness.generation_manager.all_generations()
        ]

    def budgets(self) -> dict[str, Any]:
        """The harness's default run budget, with enforcement modes made explicit.

        Per-run consumption lives on the run's own governor
        (``HarnessRunContext.budget``, whose ``to_dict`` carries the same enforcement
        facts); this reports the policy the harness applies by default.

        ``enforced`` dimensions are hard guarantees at Chassis-owned boundaries.
        ``accounted`` dimensions only hold when an integration reports usage through
        :meth:`~chassis.budget.governor.BudgetGovernor.record`.
        """

        limits = self._harness.default_budget_limits
        return {
            "default_limits": limits.to_dict(),
            "dimensions": limits.describe(),
            "enforced": [dimension.value for dimension in limits.enforced_dimensions()],
            "accounted": [dimension.value for dimension in limits.accounted_dimensions()],
            "requires_accounting": limits.requires_accounting,
        }

    def generation_pressure(self) -> GenerationPressureReport:
        """Liveness, leases, age, and retained work of every live generation.

        Answers, without taking the control-plane lock: what is current, how many
        generations are live or draining, how old each is, how old the oldest
        outstanding lease is, and which plugin instances an old generation still
        retains. It observes; it never enforces a limit.
        """

        manager = self._harness.generation_manager
        now = time.time()
        live = manager.live()
        current = manager.current
        entries: list[GenerationPressureEntry] = []
        instance_generations: dict[str, list[tuple[int, str]]] = {}
        oldest: float | None = None
        total = 0
        for generation in live:
            retained: list[dict[str, Any]] = []
            for instance in generation.instances:
                retained.append(
                    {
                        "entry_id": instance.entry_id,
                        "instance_id": instance.instance_id,
                        "plugin": instance.manifest.name,
                        "version": instance.manifest.version,
                        "state": instance.state.value,
                        "generation_refs": instance.generation_refs,
                    }
                )
                instance_generations.setdefault(instance.instance_id, []).append(
                    (generation.sequence, generation.generation_id)
                )
            lease_age = generation.oldest_lease_age_seconds
            if lease_age is not None:
                oldest = lease_age if oldest is None else max(oldest, lease_age)
            total += generation.lease_count
            entries.append(
                GenerationPressureEntry(
                    generation_id=generation.generation_id,
                    sequence=generation.sequence,
                    state=generation.state.value,
                    is_current=generation is current,
                    age_seconds=max(0.0, now - generation.created_at),
                    leases=generation.lease_count,
                    oldest_lease_age_seconds=lease_age,
                    retained_plugins=tuple(retained),
                )
            )
        entries.sort(key=lambda entry: entry.sequence, reverse=True)
        return GenerationPressureReport(
            current_generation_id=None if current is None else current.generation_id,
            live_generations=len(live),
            draining_generations=len(manager.draining()),
            total_leases=total,
            oldest_lease_age_seconds=oldest,
            generations=tuple(entries),
            instance_generations={
                key: tuple(
                    generation_id for _sequence, generation_id in sorted(value, reverse=True)
                )
                for key, value in instance_generations.items()
            },
            history_limit=manager.history_limit,
            history_retained=len(manager.history),
            history_evicted=manager.evicted,
            generated_at=now,
        )

    def instance_generations(self, instance_id: str) -> tuple[str, ...]:
        """Live generations that can currently reach a plugin instance, newest first.

        Empty when the instance is not reachable from any live generation, which
        is exactly the condition that makes it disposable.
        """

        return self.generation_pressure().instance_generations.get(instance_id, ())

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
            "tools": len(self._harness.tools),
            "hooks": len(self._harness.hooks),
            "agents": len(self._harness.agents),
            "composition": {
                "eligible": len(plan.activation_order),
                "pending": len(plan.pending),
                "cycles": len(plan.cycles),
                "generation": self._harness.current_generation.generation_id
                if self._harness.current_generation is not None
                else None,
                "draining": len(self._harness.generation_manager.draining()),
            },
            "failures": [failure.to_dict() for failure in self._harness.last_cleanup_failures],
        }

    def plugin(self, entry_id: str) -> dict[str, Any] | None:
        """Diagnostics for a single plugin entry."""

        for payload in self.plugins():
            if payload["entry_id"] == entry_id:
                return payload
        return None
