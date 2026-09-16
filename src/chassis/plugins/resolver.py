"""Reactive dependency resolution.

The resolver answers one question: *given the desired plugins and the providers
that are currently available, which plugins may activate, in what order, and why
are the others pending?*

It is a pure function of its inputs -- no mounting happens here -- so the same
inputs always produce the same plan (invariant I12). Provider selection is
deterministic, ambiguous selection is reported rather than resolved arbitrarily,
and dependency cycles are detected on the declared graph.

Two rules make the composition well-defined:

1. A plugin cannot satisfy its own requirement; a provider must be a different
   plugin instance.
2. Providers that are already active are preferred over providers that merely
   declare the capability, which keeps reconciliation stable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from packaging.version import Version

from chassis.capabilities.keys import CapabilityKey, CapabilityRequirement
from chassis.capabilities.registry import CapabilityRegistration
from chassis.core.errors import ConfigurationError, PluginCycleError
from chassis.plugins.manifest import PluginManifest

__all__ = [
    "DependencyResolver",
    "PluginCandidate",
    "PluginPlan",
    "ProviderOption",
    "RequirementResolution",
    "RequirementStatus",
    "ResolutionPlan",
]

RequirementStatus = Literal[
    "resolved", "no_provider", "version_mismatch", "ambiguous", "self_reference"
]

PlanStatus = Literal["eligible", "pending"]


@dataclass(frozen=True, slots=True)
class PluginCandidate:
    """One plugin considered for the next composition.

    Args:
        entry_id: Stable desired-state identity.
        manifest: Declared capabilities and requirements.
        instance_id: Identifier of the mounted instance, when one exists.
        active: Whether the instance is currently active and therefore already
            providing its capabilities.
        registrations: Live capability registrations. Required for active
            instances: the registry, not the manifest, is authoritative about
            what an active plugin actually provides.
    """

    entry_id: str
    manifest: PluginManifest
    instance_id: str | None = None
    active: bool = False
    registrations: tuple[CapabilityRegistration, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderOption:
    """A provider that could satisfy a requirement."""

    entry_id: str
    instance_id: str | None
    provider_name: str
    key: CapabilityKey
    version: Version
    active: bool


@dataclass(frozen=True, slots=True)
class RequirementResolution:
    """Why a requirement is satisfied or not."""

    requirement: CapabilityRequirement
    status: RequirementStatus
    provider_entry_id: str | None = None
    provider_instance_id: str | None = None
    provider_name: str | None = None
    provider_version: str | None = None
    explain: str = ""

    @property
    def satisfied(self) -> bool:
        return self.status == "resolved"

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement": str(self.requirement),
            "capability": self.requirement.name,
            "optional": self.requirement.optional,
            "status": self.status,
            "provider_entry_id": self.provider_entry_id,
            "provider_name": self.provider_name,
            "provider_version": self.provider_version,
            "explain": self.explain,
        }


@dataclass(frozen=True, slots=True)
class PluginPlan:
    """Resolved plan for one plugin entry."""

    entry_id: str
    manifest: PluginManifest
    status: PlanStatus
    instance_id: str | None
    active: bool
    order: int | None
    requirements: tuple[RequirementResolution, ...]
    reasons: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return self.status == "eligible"

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "plugin": self.manifest.identity,
            "status": self.status,
            "instance_id": self.instance_id,
            "active": self.active,
            "order": self.order,
            "requirements": [item.to_dict() for item in self.requirements],
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class ResolutionPlan:
    """Complete resolution result for one composition attempt."""

    plugins: tuple[PluginPlan, ...]
    activation_order: tuple[str, ...]
    pending: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]
    cycles: tuple[tuple[str, ...], ...]

    def plan_for(self, entry_id: str) -> PluginPlan | None:
        for plan in self.plugins:
            if plan.entry_id == entry_id:
                return plan
        return None

    def raise_for_cycles(self) -> None:
        """Raise :class:`PluginCycleError` if the declared graph has cycles."""

        if self.cycles:
            rendered = [" -> ".join((*cycle, cycle[0])) for cycle in self.cycles]
            raise PluginCycleError(
                "dependency cycle detected: " + "; ".join(rendered),
                cycles=rendered,
            )

    def explain(self, entry_id: str) -> str:
        """Multi-line explanation of a plugin's resolution state."""

        plan = self.plan_for(entry_id)
        if plan is None:
            return f"{entry_id}: not part of the desired composition"
        lines = [f"{plan.entry_id} ({plan.manifest.identity}): {plan.status}"]
        for resolution in plan.requirements:
            marker = "ok" if resolution.satisfied else resolution.status
            optional = " (optional)" if resolution.requirement.optional else ""
            lines.append(f"  {resolution.requirement}{optional}: {marker} - {resolution.explain}")
        for reason in plan.reasons:
            lines.append(f"  reason: {reason}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "activation_order": list(self.activation_order),
            "pending": list(self.pending),
            "edges": [list(edge) for edge in self.edges],
            "cycles": [list(cycle) for cycle in self.cycles],
            "plugins": [plan.to_dict() for plan in self.plugins],
        }


class DependencyResolver:
    """Computes an activation plan from desired plugins and available providers."""

    def resolve(
        self,
        candidates: Sequence[PluginCandidate],
        *,
        prefer: Mapping[str, str] | None = None,
    ) -> ResolutionPlan:
        """Resolve a composition.

        Args:
            candidates: Desired plugins with their live registrations.
            prefer: Explicit provider selection. Keys are either a capability name
                (``"database"``) or ``"<consumer entry id>:<capability>"``; values
                are provider entry ids. Used only to disambiguate.
        """

        selection = dict(prefer or {})
        ordered = _ordered_candidates(candidates)
        by_entry = _by_entry(ordered)

        declared_by_name, declared_by_entry = _declared_options(ordered)
        options_by_entry = {
            candidate.entry_id: (
                _registration_options(candidate)
                if candidate.active
                else declared_by_entry.get(candidate.entry_id, ())
            )
            for candidate in ordered
        }
        cycles = _detect_cycles(ordered, declared_by_name)
        cycle_members = frozenset(entry for cycle in cycles for entry in cycle)

        # Greatest fixpoint: start from "every desired plugin is eligible" and
        # remove the ones whose hard requirements cannot be met by the plugins
        # that remain. Removing a provider therefore also removes its consumers,
        # which is exactly the dependency cascade that unload requires.
        eligible: set[str] = set(by_entry)
        resolutions: dict[str, tuple[RequirementResolution, ...]] = {}
        while True:
            pool = _pool(options_by_entry, eligible)
            changed = False
            for candidate in ordered:
                if candidate.entry_id not in eligible:
                    continue
                candidate_resolutions = self._resolve_requirements(candidate, pool, selection)
                resolutions[candidate.entry_id] = candidate_resolutions
                if any(
                    not resolution.satisfied and not resolution.requirement.optional
                    for resolution in candidate_resolutions
                ):
                    eligible.discard(candidate.entry_id)
                    changed = True
            if not changed:
                break

        eligible_resolutions = {
            entry_id: resolutions[entry_id] for entry_id in eligible if entry_id in resolutions
        }
        edges = _dependency_edges(eligible_resolutions)
        activation_order = _activation_order(tuple(eligible), edges, cycle_members)

        plans: list[PluginPlan] = []
        for position, entry_id in enumerate(activation_order):
            candidate = by_entry[entry_id]
            plans.append(
                PluginPlan(
                    entry_id=entry_id,
                    manifest=candidate.manifest,
                    status="eligible",
                    instance_id=candidate.instance_id,
                    active=candidate.active,
                    order=position,
                    requirements=eligible_resolutions[entry_id],
                    reasons=(),
                )
            )

        pending_ids = tuple(
            candidate.entry_id
            for candidate in ordered
            if candidate.entry_id not in eligible and candidate.entry_id not in cycle_members
        )
        for entry_id in pending_ids:
            candidate = by_entry[entry_id]
            pending_resolutions = self._resolve_requirements(candidate, pool, selection)
            reasons = tuple(
                resolution.explain
                for resolution in pending_resolutions
                if not resolution.satisfied and not resolution.requirement.optional
            )
            plans.append(
                PluginPlan(
                    entry_id=entry_id,
                    manifest=candidate.manifest,
                    status="pending",
                    instance_id=candidate.instance_id,
                    active=candidate.active,
                    order=None,
                    requirements=pending_resolutions,
                    reasons=reasons,
                )
            )

        for entry_id in sorted(cycle_members):
            candidate = by_entry[entry_id]
            plans.append(
                PluginPlan(
                    entry_id=entry_id,
                    manifest=candidate.manifest,
                    status="pending",
                    instance_id=candidate.instance_id,
                    active=candidate.active,
                    order=None,
                    requirements=self._resolve_requirements(candidate, pool, selection),
                    reasons=("dependency cycle",),
                )
            )

        return ResolutionPlan(
            plugins=tuple(plans),
            activation_order=activation_order,
            pending=tuple(sorted((*pending_ids, *cycle_members))),
            edges=edges,
            cycles=cycles,
        )

    # ------------------------------------------------------------------ internals

    def _resolve_requirements(
        self,
        candidate: PluginCandidate,
        pool: Mapping[str, list[ProviderOption]],
        selection: Mapping[str, str],
    ) -> tuple[RequirementResolution, ...]:
        requirements = (
            *candidate.manifest.required_capabilities(),
            *candidate.manifest.optional_capabilities(),
        )
        return tuple(
            self._resolve_one(candidate, requirement, pool, selection)
            for requirement in requirements
        )

    def _resolve_one(
        self,
        candidate: PluginCandidate,
        requirement: CapabilityRequirement,
        pool: Mapping[str, list[ProviderOption]],
        selection: Mapping[str, str],
    ) -> RequirementResolution:
        options = tuple(
            option
            for option in pool.get(requirement.name, ())
            if option.entry_id != candidate.entry_id
        )
        matching = tuple(
            sorted(
                (option for option in options if requirement.accepts(option.key, option.version)),
                key=_option_order,
            )
        )

        if not matching:
            if options:
                offered = ", ".join(f"{option.entry_id}@{option.version}" for option in options)
                return _unresolved(
                    requirement,
                    "version_mismatch",
                    f"no provider satisfies the requirement; registered: {offered}",
                )
            if any(key.name == requirement.name for key in candidate.manifest.provided_keys()):
                return _unresolved(
                    requirement,
                    "self_reference",
                    "the only declared provider is the consumer itself",
                )
            return _unresolved(
                requirement,
                "no_provider",
                f"no provider for {requirement.name!r}",
            )

        if len(matching) == 1:
            return _resolved(requirement, matching[0])

        preferred = _preference(selection, candidate.entry_id, requirement.name)
        if preferred is not None:
            for option in matching:
                if option.entry_id == preferred:
                    return _resolved(requirement, option)

        names = ", ".join(option.entry_id for option in matching)
        return _unresolved(
            requirement,
            "ambiguous",
            f"ambiguous provider selection among {names}; "
            f"select one explicitly with provider preference",
        )


# ---------------------------------------------------------------------- helpers


def _ordered_candidates(candidates: Sequence[PluginCandidate]) -> tuple[PluginCandidate, ...]:
    ordered = tuple(sorted(candidates, key=lambda candidate: candidate.entry_id))
    seen: set[str] = set()
    for candidate in ordered:
        if candidate.entry_id in seen:
            raise ConfigurationError(
                "duplicate desired plugin entry id", entry_id=candidate.entry_id
            )
        seen.add(candidate.entry_id)
    return ordered


def _by_entry(candidates: tuple[PluginCandidate, ...]) -> dict[str, PluginCandidate]:
    return {candidate.entry_id: candidate for candidate in candidates}


def _declared_options(
    candidates: tuple[PluginCandidate, ...],
) -> tuple[
    dict[str, tuple[ProviderOption, ...]],
    dict[str, tuple[ProviderOption, ...]],
]:
    """Options derived from manifests, indexed by capability name and by entry.

    A not-yet-mounted plugin contributes what it declares it will provide; an
    already-active plugin contributes what it actually registered.
    """

    by_name: dict[str, list[ProviderOption]] = {}
    by_entry: dict[str, list[ProviderOption]] = {}
    for candidate in candidates:
        for name, version in sorted(candidate.manifest.provides.items()):
            option = ProviderOption(
                entry_id=candidate.entry_id,
                instance_id=candidate.instance_id,
                provider_name=candidate.manifest.name,
                key=CapabilityKey.from_version(name, version),
                version=Version(version),
                active=candidate.active,
            )
            by_name.setdefault(name, []).append(option)
            by_entry.setdefault(candidate.entry_id, []).append(option)
    return (
        {name: tuple(options) for name, options in by_name.items()},
        {entry: tuple(options) for entry, options in by_entry.items()},
    )


def _registration_options(candidate: PluginCandidate) -> tuple[ProviderOption, ...]:
    """Options derived from the live registrations of an active instance.

    The registry, not the manifest, is authoritative about what an active plugin
    actually provides.
    """

    return tuple(
        ProviderOption(
            entry_id=candidate.entry_id,
            instance_id=candidate.instance_id,
            provider_name=registration.provider_name,
            key=registration.key,
            version=registration.version,
            active=True,
        )
        for registration in candidate.registrations
    )


def _pool(
    options_by_entry: Mapping[str, tuple[ProviderOption, ...]],
    eligible: set[str],
) -> dict[str, list[ProviderOption]]:
    """Provider options offered by the plugins that are still eligible."""

    index: dict[str, list[ProviderOption]] = {}
    for entry_id in sorted(eligible):
        for option in options_by_entry.get(entry_id, ()):
            index.setdefault(option.key.name, []).append(option)
    return index


def _option_order(option: ProviderOption) -> tuple[str, str, str, str]:
    return (option.provider_name, option.entry_id, option.instance_id or "", str(option.version))


def _preference(selection: Mapping[str, str], entry_id: str, capability: str) -> str | None:
    scoped = selection.get(f"{entry_id}:{capability}")
    if scoped is not None:
        return scoped
    return selection.get(capability)


def _resolved(requirement: CapabilityRequirement, option: ProviderOption) -> RequirementResolution:
    return RequirementResolution(
        requirement=requirement,
        status="resolved",
        provider_entry_id=option.entry_id,
        provider_instance_id=option.instance_id,
        provider_name=option.provider_name,
        provider_version=str(option.version),
        explain=f"provided by {option.entry_id} ({option.provider_name} {option.version})",
    )


def _unresolved(
    requirement: CapabilityRequirement, status: RequirementStatus, explain: str
) -> RequirementResolution:
    return RequirementResolution(requirement=requirement, status=status, explain=explain)


def _dependency_edges(
    eligible: Mapping[str, tuple[RequirementResolution, ...]],
) -> tuple[tuple[str, str], ...]:
    edges: set[tuple[str, str]] = set()
    for entry_id, resolutions in eligible.items():
        for resolution in resolutions:
            provider = resolution.provider_entry_id
            if provider is None or provider == entry_id:
                continue
            edges.add((provider, entry_id))
    return tuple(sorted(edges))


def _activation_order(
    eligible: tuple[str, ...],
    edges: tuple[tuple[str, str], ...],
    excluded: frozenset[str],
) -> tuple[str, ...]:
    """Kahn topological order: providers before consumers, ties broken by entry id."""

    nodes = tuple(sorted(entry for entry in eligible if entry not in excluded))
    incoming: dict[str, set[str]] = {entry: set() for entry in nodes}
    outgoing: dict[str, set[str]] = {entry: set() for entry in nodes}
    for provider, consumer in edges:
        if provider in incoming and consumer in incoming:
            incoming[consumer].add(provider)
            outgoing[provider].add(consumer)

    ready = sorted(entry for entry in nodes if not incoming[entry])
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for consumer in sorted(outgoing[current]):
            incoming[consumer].discard(current)
            if not incoming[consumer] and consumer not in order and consumer not in ready:
                ready.append(consumer)
        ready.sort()

    # A cycle among eligible nodes cannot occur (cycle members are excluded from
    # eligibility), but never silently drop nodes if one is discovered.
    if len(order) != len(nodes):
        order.extend(entry for entry in nodes if entry not in order)
    return tuple(order)


def _detect_cycles(
    candidates: tuple[PluginCandidate, ...],
    declared: Mapping[str, tuple[ProviderOption, ...]],
) -> tuple[tuple[str, ...], ...]:
    """Strongly connected components of size > 1 in the declared dependency graph."""

    nodes = tuple(candidate.entry_id for candidate in candidates)
    adjacency: dict[str, set[str]] = {entry: set() for entry in nodes}
    for candidate in candidates:
        for requirement in candidate.manifest.required_capabilities():
            for option in declared.get(requirement.name, ()):
                if option.entry_id == candidate.entry_id:
                    continue
                if requirement.accepts(option.key, option.version):
                    adjacency[option.entry_id].add(candidate.entry_id)

    components = _strongly_connected_components(nodes, adjacency)
    cycles = tuple(
        sorted(tuple(sorted(component)) for component in components if len(component) > 1)
    )
    return cycles


def _strongly_connected_components(
    nodes: tuple[str, ...], adjacency: Mapping[str, set[str]]
) -> tuple[tuple[str, ...], ...]:
    """Iterative Tarjan SCC."""

    index_counter = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[tuple[str, ...]] = []

    for root in sorted(nodes):
        if root in indices:
            continue
        work: list[tuple[str, list[str]]] = [(root, sorted(adjacency.get(root, ())))]
        while work:
            node, neighbours = work[-1]
            if node not in indices:
                indices[node] = lowlink[node] = index_counter
                index_counter += 1
                stack.append(node)
                on_stack.add(node)

            advanced = False
            while neighbours:
                neighbour = neighbours.pop(0)
                if neighbour not in indices:
                    work.append((neighbour, sorted(adjacency.get(neighbour, ()))))
                    advanced = True
                    break
                if neighbour in on_stack:
                    lowlink[node] = min(lowlink[node], indices[neighbour])
            if advanced:
                continue

            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == indices[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(tuple(sorted(component)))

    return tuple(components)
