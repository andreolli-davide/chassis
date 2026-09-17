"""Read-only diagnostics.

Diagnostics are generated from authoritative runtime state, never scraped from
logs: an operator should not have to reverse-engineer why a plugin is inactive.
The surface is deliberately small and always redacted -- plugin configuration
values are described by key, never by value, because configuration may carry
secrets.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from chassis.composition import ResolvedScope, ScopeTree
from chassis.core.errors import HarnessStateError
from chassis.plugins.resolver import ProviderAssessment, RequirementResolution

if TYPE_CHECKING:
    from chassis.harness import Harness
    from chassis.plugins.lifecycle import PluginInstance

__all__ = [
    "CompositionChange",
    "Diagnostics",
    "GenerationDiff",
    "GenerationPressureEntry",
    "GenerationPressureReport",
    "RequirementExplanation",
    "ScopeExplanation",
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


_REJECTION_REASONS = {
    "self_reference": "a plugin cannot satisfy its own requirement",
    "not_visible": "provider is outside this scope's lineage",
    "capability_not_exposed": "provider is filtered out by a capability view",
    "provider_pending": "provider is not active in this composition",
    "version_mismatch": "provider version does not satisfy the requirement",
    "contract_mismatch": "provider implements a different contract generation",
    "ambiguous": "several eligible providers; no preference declared",
    "not_selected": "an explicit preference selected another provider",
}


@dataclass(frozen=True, slots=True)
class RequirementExplanation:
    """Structured answer to "why is this requirement resolved or not?".

    Built from authoritative resolution provenance, never from formatting: every
    candidate provider, where it lives, whether it was visible, eligible, and
    selected, and why it was rejected.
    """

    consumer: str
    consumer_kind: str
    scope: str
    capability: str
    requirement: str
    optional: bool
    status: str
    reason: str
    selected: Mapping[str, Any] | None
    candidates: tuple[ProviderAssessment, ...]
    generation_id: str | None

    def selected_candidate(self) -> Mapping[str, Any] | None:
        return self.selected

    def to_dict(self) -> dict[str, Any]:
        return {
            "consumer": self.consumer,
            "consumer_kind": self.consumer_kind,
            "scope": self.scope,
            "capability": self.capability,
            "requirement": self.requirement,
            "optional": self.optional,
            "status": self.status,
            "reason": self.reason,
            "selected": None if self.selected is None else dict(self.selected),
            "candidates": [item.to_dict() for item in self.candidates],
            "generation_id": self.generation_id,
        }

    def to_text(self) -> str:
        """Human-readable rendering. ``to_dict()`` stays the structured form."""

        lines = [
            f"consumer: {self.consumer} ({self.consumer_kind})",
            f"scope: {self.scope}",
            f"requirement: {self.requirement}",
        ]
        if self.generation_id:
            lines.append(f"generation: {self.generation_id}")
        lines.append("")
        lines.append("candidates:")
        if not self.candidates:
            lines.append("  (none)")
        for candidate in self.candidates:
            lines.append("")
            lines.append(
                f"{candidate.provider_entry_id} ({candidate.provider_name} {candidate.version})"
            )
            origin = candidate.origin
            where = f"{origin} ({candidate.scope})" if origin != "local" else "local"
            lines.append(f"  origin: {where}")
            if candidate.selected:
                lines.append(f"  selected: {self.reason}")
            elif candidate.eligible:
                lines.append("  eligible: yes")
            elif candidate.rejection:
                detail = _REJECTION_REASONS.get(candidate.rejection, candidate.rejection)
                lines.append(f"  rejected: {candidate.rejection} ({detail})")
        if not self.candidates or not any(item.selected for item in self.candidates):
            lines.append("")
            lines.append(f"unresolved: {self.status} ({self.reason})")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ScopeExplanation:
    """Structured answer to "what can this scope observe and what does it own?"."""

    path: str
    name: str
    parent: str | None
    children: tuple[str, ...]
    capabilities: tuple[str, ...] | None
    entries: tuple[str, ...]
    instances: tuple[str, ...]
    local_providers: Mapping[str, tuple[str, ...]]
    inherited_providers: Mapping[str, tuple[str, ...]]
    visible_providers: Mapping[str, tuple[str, ...]]
    requirements: tuple[RequirementExplanation, ...]
    unresolved: tuple[str, ...]
    owned_registrations: tuple[Mapping[str, Any], ...]
    tools: tuple[str, ...]
    hooks: tuple[Mapping[str, Any], ...]
    metadata: Mapping[str, Any]
    generation_id: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "local_providers", MappingProxyType(dict(self.local_providers)))
        object.__setattr__(
            self, "inherited_providers", MappingProxyType(dict(self.inherited_providers))
        )
        object.__setattr__(
            self, "visible_providers", MappingProxyType(dict(self.visible_providers))
        )
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "parent": self.parent,
            "children": list(self.children),
            "capabilities": None if self.capabilities is None else list(self.capabilities),
            "entries": list(self.entries),
            "instances": list(self.instances),
            "local_providers": {
                name: list(ids) for name, ids in sorted(self.local_providers.items())
            },
            "inherited_providers": {
                name: list(ids) for name, ids in sorted(self.inherited_providers.items())
            },
            "visible_providers": {
                name: list(ids) for name, ids in sorted(self.visible_providers.items())
            },
            "requirements": [item.to_dict() for item in self.requirements],
            "unresolved": list(self.unresolved),
            "owned_registrations": [dict(item) for item in self.owned_registrations],
            "tools": list(self.tools),
            "hooks": [dict(item) for item in self.hooks],
            "metadata": dict(sorted(self.metadata.items(), key=lambda item: str(item[0]))),
            "generation_id": self.generation_id,
        }

    def to_text(self) -> str:
        lines = [f"scope: {self.path}"]
        lines.append(f"  parent: {self.parent or '(none)'}")
        lines.append(f"  children: {', '.join(self.children) or '(none)'}")
        lines.append(
            "  capabilities: "
            + ("(unrestricted)" if self.capabilities is None else ", ".join(self.capabilities))
        )
        lines.append(f"  entries: {', '.join(self.entries) or '(none)'}")
        lines.append(f"  instances: {', '.join(self.instances) or '(none)'}")
        for label, mapping in (
            ("local providers", self.local_providers),
            ("inherited providers", self.inherited_providers),
        ):
            lines.append(f"  {label}:")
            if not mapping:
                lines.append("    (none)")
            for name, ids in sorted(mapping.items()):
                lines.append(f"    {name}: {', '.join(ids)}")
        lines.append("  requirements:")
        if not self.requirements:
            lines.append("    (none)")
        for requirement in self.requirements:
            marker = "ok" if requirement.status == "resolved" else requirement.status
            lines.append(f"    {requirement.consumer} {requirement.requirement}: {marker}")
        if self.unresolved:
            lines.append(f"  unresolved: {', '.join(self.unresolved)}")
        lines.append(f"  owned registrations: {len(self.owned_registrations)}")
        lines.append(f"  tools: {', '.join(self.tools) or '(none)'}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class CompositionChange:
    """One semantic difference between two compositions."""

    category: str
    kind: str
    subject: str
    old: str | None = None
    new: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "kind": self.kind,
            "subject": self.subject,
            "old": self.old,
            "new": self.new,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class GenerationDiff:
    """Semantic diff between two published generations.

    The diff describes *composition*, not Python objects. It is conservative: two
    resources are reported as reused only when their entry id, instance id, and
    implementation identity all match, so it never claims semantic equivalence it
    cannot prove.
    """

    old_generation_id: str
    new_generation_id: str
    changes: tuple[CompositionChange, ...]
    include_unchanged: bool = False

    def by_category(self, category: str) -> tuple[CompositionChange, ...]:
        return tuple(item for item in self.changes if item.category == category)

    @property
    def scopes(self) -> tuple[CompositionChange, ...]:
        return self.by_category("scope")

    @property
    def providers(self) -> tuple[CompositionChange, ...]:
        return self.by_category("provider")

    @property
    def requirements(self) -> tuple[CompositionChange, ...]:
        return self.by_category("requirement")

    @property
    def is_empty(self) -> bool:
        return not self.changes

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_generation_id": self.old_generation_id,
            "new_generation_id": self.new_generation_id,
            "changes": [item.to_dict() for item in self.changes],
        }

    def to_text(self) -> str:
        lines = [f"{self.old_generation_id} -> {self.new_generation_id}", ""]
        for category, title in (
            ("scope", "SCOPES"),
            ("provider", "PROVIDERS"),
            ("requirement", "REQUIREMENTS"),
        ):
            items = self.by_category(category)
            if not items:
                continue
            lines.append(title)
            for kind in ("added", "removed", "replaced", "changed", "rewired", "unchanged"):
                group = [item for item in items if item.kind == kind]
                if not group:
                    continue
                lines.append(f"  {kind.upper()}")
                for item in group:
                    if kind in ("replaced", "rewired", "changed") and item.old is not None:
                        lines.append(f"    {item.subject}: {item.old} -> {item.new}")
                    else:
                        lines.append(f"    {item.subject}")
                    if item.reason:
                        lines.append(f"      reason: {item.reason}")
            lines.append("")
        if len(lines) == 2:
            lines.append("(no composition changes)")
        return "\n".join(lines).rstrip()


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
                    "scope": entry.scope,
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
            "scopes": [scope.to_dict() for scope in plan.scopes],
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

    # ------------------------------------------------------- scoped composition

    def scopes(self, *, generation_id: str | None = None) -> list[dict[str, Any]]:
        """The resolved scope tree of a generation, in deterministic pre-order."""

        return [scope.to_dict() for scope in self._scope_tree(generation_id)]

    def explain_requirement(
        self,
        consumer: str,
        capability: str,
        *,
        scope: str | None = None,
        generation_id: str | None = None,
    ) -> RequirementExplanation | None:
        """Why a requirement resolved to a provider, or why it is unresolved.

        Args:
            consumer: Entry id of the plugin whose requirement this is, or the
                scope path itself for a scope-local requirement.
            capability: Capability name.
            scope: Restrict the search to requirements owned by this scope.
            generation_id: Explain a published generation instead of the current
                desired state. Defaults to the desired-state plan, which also
                explains requirements whose consumer is still pending.

        Returns:
            Structured provenance, or ``None`` when no such requirement exists.
        """

        if generation_id is not None:
            scopes: Sequence[Any] = self._lookup_generation(generation_id).scopes.scopes
            resolved_generation_id: str | None = generation_id
        else:
            scopes = self._harness.plan().scopes
            current = self._harness.current_generation
            resolved_generation_id = None if current is None else current.generation_id
        for scope_plan in scopes:
            if scope is not None and scope_plan.path != scope:
                continue
            for resolution in scope_plan.provenance:
                if resolution.consumer != consumer or resolution.requirement.name != capability:
                    continue
                return self._explain_resolution(resolution, scope_plan.path, resolved_generation_id)
        return None

    def explain_scope(self, path: str, *, generation_id: str | None = None) -> ScopeExplanation:
        """What a scope may observe, what it owns, and what it is missing.

        Visibility comes from the resolved composition; ownership comes from the
        published generation's instances. Metadata values are redacted and
        configuration values are never reported.
        """

        plan_scope = self._harness.plan().scope_for(path)
        if plan_scope is None:
            raise HarnessStateError("unknown composition scope", path=path)
        generation = (
            self._lookup_generation(generation_id)
            if generation_id is not None
            else self._harness.current_generation
        )
        resolved = None if generation is None else generation.scopes.get(path)
        if resolved is not None and generation is not None:
            entry_by_instance = {
                instance.instance_id: instance.entry_id for instance in generation.instances
            }
            local = self._display_providers(resolved.providers, entry_by_instance)
            inherited = self._display_providers(resolved.inherited, entry_by_instance)
            visible = self._display_providers(resolved.visible, entry_by_instance)
            instances = tuple(
                sorted(
                    entry_by_instance.get(instance_id, instance_id)
                    for instance_id in resolved.instances
                )
            )
            metadata = dict(resolved.metadata)
            owned, tools, hooks = self._owned_effects(generation, resolved)
            child_paths = resolved.children
            capabilities = resolved.capabilities
            entries = resolved.entries
        else:
            local = dict(plan_scope.providers)
            inherited = dict(plan_scope.inherited)
            visible = dict(plan_scope.visible)
            instances = ()
            metadata = dict(plan_scope.metadata)
            owned, tools, hooks = (), (), ()
            child_paths = plan_scope.children
            capabilities = plan_scope.capabilities
            entries = plan_scope.entries
        explanations = tuple(
            self._explain_resolution(
                resolution,
                path,
                None if generation is None else generation.generation_id,
            )
            for resolution in plan_scope.provenance
        )
        unresolved = tuple(
            f"{item.consumer} {item.requirement}"
            for item in plan_scope.provenance
            if not item.satisfied and not item.requirement.optional
        )
        return ScopeExplanation(
            path=path,
            name=plan_scope.name,
            parent=plan_scope.parent,
            children=child_paths,
            capabilities=capabilities,
            entries=entries,
            instances=instances,
            local_providers=local,
            inherited_providers=inherited,
            visible_providers=visible,
            requirements=explanations,
            unresolved=unresolved,
            owned_registrations=owned,
            tools=tools,
            hooks=hooks,
            metadata=self._harness.redactor.redact_value(metadata),
            generation_id=None if generation is None else generation.generation_id,
        )

    def diff_generations(
        self,
        old_id: str,
        new_id: str,
        *,
        include_unchanged: bool = False,
    ) -> GenerationDiff:
        """Semantic composition diff between two published generations.

        Reports scopes, providers, and requirement wiring. Reuse is claimed only
        when entry id, instance id, and implementation identity all match, so the
        diff is conservative rather than semantically speculative.
        """

        old = self._lookup_generation(old_id)
        new = self._lookup_generation(new_id)
        changes = [
            *self._diff_scopes(old, new, include_unchanged=include_unchanged),
            *self._diff_providers(old, new, include_unchanged=include_unchanged),
            *self._diff_requirements(old, new, include_unchanged=include_unchanged),
        ]
        changes.sort(key=lambda item: (item.category, item.kind, item.subject))
        return GenerationDiff(
            old_generation_id=old_id,
            new_generation_id=new_id,
            changes=tuple(changes),
            include_unchanged=include_unchanged,
        )

    # ------------------------------------------------- composition explain helpers

    def _scope_tree(self, generation_id: str | None) -> ScopeTree:
        generation = (
            self._lookup_generation(generation_id)
            if generation_id is not None
            else self._harness.current_generation
        )
        return ScopeTree.root_only() if generation is None else generation.scopes

    def _lookup_generation(self, generation_id: str) -> Any:
        for generation in self._harness.generation_manager.all_generations():
            if generation.generation_id == generation_id:
                return generation
        raise HarnessStateError("unknown runtime generation", generation_id=generation_id)

    def _explain_resolution(
        self, resolution: RequirementResolution, scope: str, generation_id: str | None
    ) -> RequirementExplanation:
        selected: dict[str, Any] | None = None
        if resolution.satisfied:
            selected = {
                "provider_entry_id": resolution.provider_entry_id,
                "provider_instance_id": resolution.provider_instance_id,
                "provider_name": resolution.provider_name,
                "provider_version": resolution.provider_version,
                "scope": resolution.provider_scope,
                "origin": resolution.provider_origin,
            }
        return RequirementExplanation(
            consumer=resolution.consumer,
            consumer_kind=resolution.consumer_kind,
            scope=scope,
            capability=resolution.requirement.name,
            requirement=str(resolution.requirement),
            optional=resolution.requirement.optional,
            status=resolution.status,
            reason=resolution.selection_reason or resolution.explain,
            selected=selected,
            candidates=resolution.assessments,
            generation_id=generation_id,
        )

    @staticmethod
    def _display_providers(
        mapping: Mapping[str, tuple[str, ...]], entry_by_instance: Mapping[str, str]
    ) -> dict[str, tuple[str, ...]]:
        return {
            name: tuple(entry_by_instance.get(instance_id, instance_id) for instance_id in ids)
            for name, ids in mapping.items()
        }

    def _owned_effects(
        self, generation: Any, scope: ResolvedScope
    ) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...], tuple[Mapping[str, Any], ...]]:
        instance_ids = set(scope.instances)
        registrations = tuple(
            registration.to_dict()
            for registration in generation.snapshot.registrations
            if registration.provider_id in instance_ids
        )
        tools = tuple(
            entry.tool.name
            for entry in self._harness.tool_snapshot(generation).entries
            if entry.owner_id in instance_ids
        )
        hooks = tuple(
            registration.to_dict()
            for registration in self._harness.hook_snapshot(generation).registrations
            if registration.owner_id in instance_ids
        )
        return registrations, tools, hooks

    def _diff_scopes(
        self, old: Any, new: Any, *, include_unchanged: bool
    ) -> list[CompositionChange]:
        def signature(scope: ResolvedScope) -> tuple[Any, ...]:
            """Structural scope identity: topology and capability view.

            Provider and requirement changes are reported by their own diff
            categories, so a scope is "changed" only when its structure moved.
            """

            return (scope.parent, scope.children, scope.capabilities)

        old_scopes = {scope.path: scope for scope in old.scopes}
        new_scopes = {scope.path: scope for scope in new.scopes}
        changes: list[CompositionChange] = []
        for path in sorted(set(old_scopes) | set(new_scopes)):
            before = old_scopes.get(path)
            after = new_scopes.get(path)
            if before is None and after is not None:
                changes.append(CompositionChange("scope", "added", path, new=after.name))
            elif before is not None and after is None:
                changes.append(CompositionChange("scope", "removed", path, old=before.name))
            elif before is not None and after is not None:
                if signature(before) != signature(after):
                    changes.append(
                        CompositionChange(
                            "scope",
                            "changed",
                            path,
                            old=before.name,
                            new=after.name,
                            reason="scope composition changed",
                        )
                    )
                elif include_unchanged:
                    changes.append(CompositionChange("scope", "unchanged", path))
        return changes

    def _diff_providers(
        self, old: Any, new: Any, *, include_unchanged: bool
    ) -> list[CompositionChange]:
        old_instances = {instance.entry_id: instance for instance in old.instances}
        new_instances = {instance.entry_id: instance for instance in new.instances}
        changes: list[CompositionChange] = []
        for entry_id in sorted(set(old_instances) | set(new_instances)):
            before = old_instances.get(entry_id)
            after = new_instances.get(entry_id)
            if before is None and after is not None:
                changes.append(
                    CompositionChange(
                        "provider",
                        "added",
                        entry_id,
                        new=f"{after.manifest.identity}#{after.instance_id}",
                    )
                )
            elif before is not None and after is None:
                changes.append(
                    CompositionChange(
                        "provider",
                        "removed",
                        entry_id,
                        old=f"{before.manifest.identity}#{before.instance_id}",
                    )
                )
            elif before is not None and after is not None:
                if (
                    before.instance_id == after.instance_id
                    and before.manifest.identity == after.manifest.identity
                ):
                    if include_unchanged:
                        changes.append(CompositionChange("provider", "unchanged", entry_id))
                else:
                    changes.append(
                        CompositionChange(
                            "provider",
                            "replaced",
                            entry_id,
                            old=f"{before.manifest.identity}#{before.instance_id}",
                            new=f"{after.manifest.identity}#{after.instance_id}",
                            reason="provider implementation or instance changed",
                        )
                    )
        return changes

    def _diff_requirements(
        self, old: Any, new: Any, *, include_unchanged: bool
    ) -> list[CompositionChange]:
        def index(generation: Any) -> dict[tuple[str, str, str], RequirementResolution]:
            collected: dict[tuple[str, str, str], RequirementResolution] = {}
            for scope in generation.scopes:
                for resolution in scope.provenance:
                    key = (scope.path, resolution.consumer, resolution.requirement.name)
                    collected[key] = resolution
            return collected

        before = index(old)
        after = index(new)
        changes: list[CompositionChange] = []
        for key in sorted(set(before) | set(after)):
            path, consumer, capability = key
            subject = f"{path}::{consumer}::{capability}"
            previous = before.get(key)
            current = after.get(key)
            if previous is None and current is not None:
                changes.append(
                    CompositionChange(
                        "requirement",
                        "added",
                        subject,
                        new=current.provider_entry_id or current.status,
                    )
                )
            elif previous is not None and current is None:
                changes.append(
                    CompositionChange(
                        "requirement",
                        "removed",
                        subject,
                        old=previous.provider_entry_id or previous.status,
                    )
                )
            elif previous is not None and current is not None:
                if (
                    previous.provider_entry_id == current.provider_entry_id
                    and previous.status == current.status
                ):
                    if include_unchanged:
                        changes.append(CompositionChange("requirement", "unchanged", subject))
                else:
                    changes.append(
                        CompositionChange(
                            "requirement",
                            "rewired",
                            subject,
                            old=previous.provider_entry_id or previous.status,
                            new=current.provider_entry_id or current.status,
                            reason=current.explain,
                        )
                    )
        return changes

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
                "scopes": len(plan.scopes),
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
