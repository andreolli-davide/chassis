"""Reactive dependency resolution over a hierarchical composition.

The resolver answers one question: *given the desired plugins, the composition
scopes they live in, and the providers that are currently available, which plugins
may activate, in what order, and why are the others pending?*

It is a pure function of its inputs -- no mounting happens here -- so the same
inputs always produce the same plan (invariant I12). Provider selection is
deterministic, ambiguous selection is reported rather than resolved arbitrarily,
and dependency cycles are detected on the declared graph.

Composition scopes enter through one rule, and only one:

    a consumer in scope ``S`` may use a provider that lives in ``S`` or in one of
    ``S`` ancestors, filtered by ``S``'s capability view.

Nothing else changes scope membership: a parent never sees a child's local
providers, and siblings never see each other's. A provider that is visible to a
consumer competes on equal terms with any other visible provider -- local is not
silently preferred over inherited, exactly as two providers in one flat
composition are not silently ordered. A valid local provider and a valid inherited
provider make the requirement ``ambiguous`` until a preference selects one.

Three rules make the composition well-defined:

1. A plugin cannot satisfy its own requirement; a provider must be a different
   plugin instance.
2. An already-active plugin contributes what it *actually registered*, while a
   not-yet-mounted plugin contributes what its manifest declares. An active
   provider is not otherwise preferred: a requirement satisfied by several
   providers is ambiguous until an explicit preference selects one, and
   incremental reuse (semantic identity), not selection preference, is what
   keeps reconciliation stable.
3. Capability narrowing only narrows: a scope's view is intersected along its
   lineage, so a descendant can never observe more than its ancestor exposes.

Every requirement resolution carries its own provenance: which providers were
considered, where they live, which were rejected and why, and why the selected one
won. Diagnostics read that structure; they never re-derive an explanation from
logs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol, runtime_checkable

from packaging.version import Version

from chassis.capabilities.keys import CapabilityKey, CapabilityRequirement
from chassis.capabilities.registry import CapabilityRegistration
from chassis.core.errors import ConfigurationError, PluginCycleError
from chassis.plugins.manifest import PluginManifest

__all__ = [
    "DependencyResolver",
    "PluginCandidate",
    "PluginPlan",
    "ProviderAssessment",
    "ProviderOption",
    "RequirementResolution",
    "RequirementStatus",
    "ResolutionPlan",
    "ScopePlan",
]

#: Root scope path. Every composition has exactly one root.
ROOT_SCOPE = "/"

RequirementStatus = Literal[
    "resolved",
    "no_provider",
    "version_mismatch",
    "ambiguous",
    "self_reference",
    "not_visible",
    "provider_pending",
]

PlanStatus = Literal["eligible", "pending"]

#: A provider is local to the consumer's scope, inherited from an ancestor, or not
#: in the consumer's lineage at all.
ProviderOrigin = Literal["local", "inherited", "unrelated"]


@runtime_checkable
class ScopeSpecLike(Protocol):
    """Structural view of a composition scope the resolver needs.

    Declared here rather than imported so the resolver stays below the
    composition module in the import graph: the resolver is a pure function and
    must not depend on the control-plane type that carries desired state.
    """

    @property
    def path(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def parent(self) -> str | None: ...

    @property
    def capabilities(self) -> frozenset[str] | None: ...

    @property
    def tools(self) -> frozenset[str] | None: ...

    @property
    def requirements(self) -> tuple[CapabilityRequirement, ...]: ...

    @property
    def metadata(self) -> Mapping[str, Any]: ...


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
        scope: Path of the composition scope that declares this entry.
    """

    entry_id: str
    manifest: PluginManifest
    instance_id: str | None = None
    active: bool = False
    registrations: tuple[CapabilityRegistration, ...] = ()
    scope: str = ROOT_SCOPE


@dataclass(frozen=True, slots=True)
class ProviderOption:
    """A provider that could satisfy a requirement."""

    entry_id: str
    instance_id: str | None
    provider_name: str
    key: CapabilityKey
    version: Version
    active: bool
    scope: str = ROOT_SCOPE


@dataclass(frozen=True, slots=True)
class ProviderAssessment:
    """Why one provider was or was not a viable candidate for one requirement.

    Recorded for every provider registered or declared for the capability, not
    only the visible ones, so an operator can see that a sibling's provider exists
    and was correctly excluded.
    """

    provider_entry_id: str
    provider_instance_id: str | None
    provider_name: str
    version: str
    scope: str
    origin: ProviderOrigin
    visible: bool
    eligible: bool
    selected: bool
    rejection: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_entry_id": self.provider_entry_id,
            "provider_instance_id": self.provider_instance_id,
            "provider_name": self.provider_name,
            "version": self.version,
            "scope": self.scope,
            "origin": self.origin,
            "visible": self.visible,
            "eligible": self.eligible,
            "selected": self.selected,
            "rejection": self.rejection,
        }


@dataclass(frozen=True, slots=True)
class RequirementResolution:
    """Why a requirement is satisfied or not, with its provenance."""

    requirement: CapabilityRequirement
    status: RequirementStatus
    provider_entry_id: str | None = None
    provider_instance_id: str | None = None
    provider_name: str | None = None
    provider_version: str | None = None
    provider_key: str | None = None
    explain: str = ""
    consumer: str = ""
    consumer_kind: str = "plugin"
    provider_scope: str | None = None
    provider_origin: ProviderOrigin | None = None
    selection_reason: str | None = None
    assessments: tuple[ProviderAssessment, ...] = ()

    @property
    def satisfied(self) -> bool:
        return self.status == "resolved"

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement": str(self.requirement),
            "capability": self.requirement.name,
            "optional": self.requirement.optional,
            "status": self.status,
            "consumer": self.consumer,
            "consumer_kind": self.consumer_kind,
            "provider_entry_id": self.provider_entry_id,
            "provider_name": self.provider_name,
            "provider_key": self.provider_key,
            "provider_version": self.provider_version,
            "provider_scope": self.provider_scope,
            "provider_origin": self.provider_origin,
            "selection_reason": self.selection_reason,
            "explain": self.explain,
            "assessments": [assessment.to_dict() for assessment in self.assessments],
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
    scope: str = ROOT_SCOPE

    @property
    def eligible(self) -> bool:
        return self.status == "eligible"

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "plugin": self.manifest.identity,
            "scope": self.scope,
            "status": self.status,
            "instance_id": self.instance_id,
            "active": self.active,
            "order": self.order,
            "requirements": [item.to_dict() for item in self.requirements],
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class ScopePlan:
    """Resolved plan for one composition scope.

    ``providers`` are the scope's own visible providers, ``inherited`` are the
    visible providers it inherits from ancestors, and ``visible`` is their union.
    ``provenance`` records every requirement resolved *in* this scope: the
    requirements of the plugins it declares, plus its own local requirements
    (``requirements``).
    """

    path: str
    name: str
    parent: str | None
    children: tuple[str, ...]
    capabilities: tuple[str, ...] | None
    entries: tuple[str, ...]
    order: tuple[str, ...]
    pending: tuple[str, ...]
    providers: Mapping[str, tuple[str, ...]]
    inherited: Mapping[str, tuple[str, ...]]
    visible: Mapping[str, tuple[str, ...]]
    requirements: tuple[RequirementResolution, ...]
    provenance: tuple[RequirementResolution, ...]
    metadata: Mapping[str, Any] = MappingProxyType({})
    tools: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        for field in ("providers", "inherited", "visible", "metadata"):
            value = getattr(self, field)
            if not isinstance(value, MappingProxyType):
                object.__setattr__(self, field, MappingProxyType(dict(value)))

    def to_dict(self) -> dict[str, Any]:
        """Structured, JSON-compatible form. Metadata is never included."""

        return {
            "path": self.path,
            "name": self.name,
            "parent": self.parent,
            "children": list(self.children),
            "capabilities": None if self.capabilities is None else list(self.capabilities),
            "tools": None if self.tools is None else list(self.tools),
            "entries": list(self.entries),
            "order": list(self.order),
            "pending": list(self.pending),
            "providers": {name: list(ids) for name, ids in sorted(self.providers.items())},
            "inherited": {name: list(ids) for name, ids in sorted(self.inherited.items())},
            "visible": {name: list(ids) for name, ids in sorted(self.visible.items())},
            "requirements": [item.to_dict() for item in self.requirements],
            "provenance": [item.to_dict() for item in self.provenance],
        }


@dataclass(frozen=True, slots=True)
class ResolutionPlan:
    """Complete resolution result for one composition attempt."""

    plugins: tuple[PluginPlan, ...]
    activation_order: tuple[str, ...]
    pending: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]
    cycles: tuple[tuple[str, ...], ...]
    scopes: tuple[ScopePlan, ...] = ()

    def plan_for(self, entry_id: str) -> PluginPlan | None:
        for plan in self.plugins:
            if plan.entry_id == entry_id:
                return plan
        return None

    def scope_for(self, path: str) -> ScopePlan | None:
        for scope in self.scopes:
            if scope.path == path:
                return scope
        return None

    def provenance(
        self, *, scope: str | None = None, consumer: str | None = None
    ) -> tuple[RequirementResolution, ...]:
        """Requirement provenance, optionally filtered by scope or consumer."""

        selected = (
            self.scopes
            if scope is None
            else tuple(item for item in self.scopes if item.path == scope)
        )
        records = tuple(resolution for item in selected for resolution in item.provenance)
        if consumer is None:
            return records
        return tuple(item for item in records if item.consumer == consumer)

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
        if plan.scope != ROOT_SCOPE:
            lines.append(f"  scope: {plan.scope}")
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
            "scopes": [scope.to_dict() for scope in self.scopes],
        }


class _RootSpec:
    """Fallback root scope used when no hierarchy is supplied."""

    __slots__ = ("capabilities", "metadata", "name", "parent", "path", "requirements", "tools")

    def __init__(
        self, path: str = ROOT_SCOPE, name: str = "root", parent: str | None = None
    ) -> None:
        self.path = path
        self.name = name
        self.parent = parent
        self.capabilities: frozenset[str] | None = None
        self.tools: frozenset[str] | None = None
        self.requirements: tuple[CapabilityRequirement, ...] = ()
        self.metadata: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class _ScopeView:
    """What one scope can observe while resolving."""

    path: str
    lineage: tuple[str, ...]
    allowed: frozenset[str] | None
    eligible: frozenset[str]
    visible: Mapping[str, tuple[ProviderOption, ...]]
    pool: Mapping[str, tuple[ProviderOption, ...]]


class _ScopeIndex:
    """Validated hierarchy of composition scope specifications."""

    def __init__(self, specs: Sequence[ScopeSpecLike] | None) -> None:
        entries = (_RootSpec(),) if specs is None else tuple(specs)
        self._specs: dict[str, ScopeSpecLike] = {}
        for spec in entries:
            if spec.path in self._specs:
                raise ConfigurationError("duplicate composition scope path", path=spec.path)
            self._specs[spec.path] = spec
        if ROOT_SCOPE not in self._specs:
            raise ConfigurationError(
                "composition scopes must include the root scope", path=ROOT_SCOPE
            )
        for spec in self._specs.values():
            if spec.parent is not None and spec.parent not in self._specs:
                raise ConfigurationError(
                    "composition scope parent is not declared",
                    path=spec.path,
                    parent=spec.parent,
                )
        self._lineages: dict[str, tuple[str, ...]] = {}
        for path in self._specs:
            self._lineages[path] = self._lineage(path)
        self._allowed = {path: self._allowed_for(path) for path in self._specs}

    def _lineage(self, path: str) -> tuple[str, ...]:
        chain: list[str] = []
        seen: set[str] = set()
        current: str | None = path
        while current is not None:
            if current in seen:  # pragma: no cover - defensive
                raise ConfigurationError("composition scope cycle detected", path=current)
            seen.add(current)
            chain.append(current)
            current = self._specs[current].parent
        return tuple(reversed(chain))

    def _allowed_for(self, path: str) -> frozenset[str] | None:
        """Capabilities a scope may observe: the intersection of its lineage views."""

        allowed: frozenset[str] | None = None
        for ancestor in self._lineages[path]:
            view = self._specs[ancestor].capabilities
            if view is None:
                continue
            allowed = view if allowed is None else allowed & view
        return allowed

    def require(self, path: str) -> None:
        if path not in self._specs:
            raise ConfigurationError(
                "plugin entry refers to an undeclared composition scope",
                path=path,
            )

    def paths(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def lineage(self, path: str) -> tuple[str, ...]:
        return self._lineages[path]

    def allowed(self, path: str) -> frozenset[str] | None:
        return self._allowed[path]

    def ordered(self) -> tuple[ScopeSpecLike, ...]:
        """Specs in pre-order, which is deterministic and stable across runs."""

        ordered: list[ScopeSpecLike] = []

        def walk(path: str) -> None:
            ordered.append(self._specs[path])
            for child in sorted(item.path for item in self._specs.values() if item.parent == path):
                walk(child)

        walk(ROOT_SCOPE)
        return tuple(ordered)

    def children(self, path: str) -> tuple[str, ...]:
        return tuple(sorted(item.path for item in self._specs.values() if item.parent == path))

    def can_see(self, consumer: str, provider: str, capability: str) -> bool:
        if provider not in self._lineages[consumer]:
            return False
        allowed = self._allowed[consumer]
        return allowed is None or capability in allowed


class DependencyResolver:
    """Computes an activation plan from desired plugins, scopes, and providers."""

    def resolve(
        self,
        candidates: Sequence[PluginCandidate],
        *,
        prefer: Mapping[str, str] | None = None,
        scopes: Sequence[ScopeSpecLike] | None = None,
    ) -> ResolutionPlan:
        """Resolve a composition.

        Args:
            candidates: Desired plugins with their live registrations and scopes.
            prefer: Explicit provider selection. Keys are a capability name
                (``"database"``), ``"<consumer entry id>:<capability>"``, or
                ``"scope:<scope path>:<capability>"``; values are provider entry
                ids. Used only to disambiguate, most specific key first.
            scopes: Composition scope hierarchy. ``None`` means a single root
                scope, which is a flat composition with unrestricted visibility.
        """

        selection = dict(prefer or {})
        ordered = _ordered_candidates(candidates)
        index = _ScopeIndex(scopes)
        for candidate in ordered:
            index.require(candidate.scope)
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
        all_options_by_name = _index_by_name(options_by_entry)
        cycles = _detect_cycles(ordered, declared_by_name, index)
        cycle_members = frozenset(entry for cycle in cycles for entry in cycle)

        # Greatest fixpoint: start from "every desired plugin is eligible" and
        # remove the ones whose hard requirements cannot be met by the plugins
        # that remain. Removing a provider therefore also removes its consumers,
        # which is exactly the dependency cascade that unload requires.
        eligible: set[str] = set(by_entry)
        resolutions: dict[str, tuple[RequirementResolution, ...]] = {}
        views: dict[str, _ScopeView] = {}
        while True:
            views = _build_views(index, ordered, options_by_entry, frozenset(eligible))
            changed = False
            for candidate in ordered:
                if candidate.entry_id not in eligible:
                    continue
                candidate_resolutions = self._resolve_requirements(
                    candidate, views[candidate.scope], selection, all_options_by_name
                )
                resolutions[candidate.entry_id] = candidate_resolutions
                if any(
                    not resolution.satisfied and not resolution.requirement.optional
                    for resolution in candidate_resolutions
                ):
                    eligible.discard(candidate.entry_id)
                    changed = True
            if not changed:
                break

        views = _build_views(index, ordered, options_by_entry, frozenset(eligible))
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
                    scope=candidate.scope,
                )
            )

        pending_ids = tuple(
            candidate.entry_id
            for candidate in ordered
            if candidate.entry_id not in eligible and candidate.entry_id not in cycle_members
        )
        for entry_id in pending_ids:
            candidate = by_entry[entry_id]
            pending_resolutions = self._resolve_requirements(
                candidate, views[candidate.scope], selection, all_options_by_name
            )
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
                    scope=candidate.scope,
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
                    requirements=self._resolve_requirements(
                        candidate, views[candidate.scope], selection, all_options_by_name
                    ),
                    reasons=("dependency cycle",),
                    scope=candidate.scope,
                )
            )

        plan_by_entry = {plan.entry_id: plan for plan in plans}
        scope_plans = self._build_scope_plans(
            index,
            ordered,
            plan_by_entry,
            views,
            activation_order,
            pending_ids,
            selection,
            all_options_by_name,
            cycle_members,
        )

        return ResolutionPlan(
            plugins=tuple(plans),
            activation_order=activation_order,
            pending=tuple(sorted((*pending_ids, *cycle_members))),
            edges=edges,
            cycles=cycles,
            scopes=scope_plans,
        )

    # ------------------------------------------------------------------ internals

    def _resolve_requirements(
        self,
        candidate: PluginCandidate,
        view: _ScopeView,
        selection: Mapping[str, str],
        all_options_by_name: Mapping[str, tuple[ProviderOption, ...]],
    ) -> tuple[RequirementResolution, ...]:
        requirements = (
            *candidate.manifest.required_capabilities(),
            *candidate.manifest.optional_capabilities(),
        )
        return tuple(
            self._resolve_one(
                consumer=candidate.entry_id,
                consumer_kind="plugin",
                requirement=requirement,
                view=view,
                selection=selection,
                all_options_by_name=all_options_by_name,
                exclude_entry=candidate.entry_id,
                provided_keys=candidate.manifest.provided_keys(),
            )
            for requirement in requirements
        )

    def _resolve_one(
        self,
        *,
        consumer: str,
        consumer_kind: str,
        requirement: CapabilityRequirement,
        view: _ScopeView,
        selection: Mapping[str, str],
        all_options_by_name: Mapping[str, tuple[ProviderOption, ...]],
        exclude_entry: str | None = None,
        provided_keys: tuple[CapabilityKey, ...] = (),
    ) -> RequirementResolution:
        visible_opts = view.visible.get(requirement.name, ())
        visible_excl = tuple(option for option in visible_opts if option.entry_id != exclude_entry)
        pool_opts = tuple(
            option
            for option in view.pool.get(requirement.name, ())
            if option.entry_id != exclude_entry
        )
        matching = tuple(
            sorted(
                (option for option in pool_opts if requirement.accepts(option.key, option.version)),
                key=_option_order,
            )
        )

        selected: ProviderOption | None = None
        selection_reason: str | None = None
        status: RequirementStatus
        explain: str

        if matching:
            if len(matching) == 1:
                selected = matching[0]
                selection_reason = "only_eligible"
                status = "resolved"
                explain = _resolved_explain(selected, view.path)
            else:
                preferred = _preference(selection, consumer, requirement.name, view.path)
                if preferred is not None:
                    for option in matching:
                        if option.entry_id == preferred:
                            selected = option
                            break
                if selected is not None:
                    selection_reason = "explicit_preference"
                    status = "resolved"
                    explain = (
                        f"{_resolved_explain(selected, view.path)} (explicit provider preference)"
                    )
                else:
                    status = "ambiguous"
                    names = ", ".join(option.entry_id for option in matching)
                    explain = (
                        f"ambiguous provider selection among {names}; "
                        f"select one explicitly with provider preference"
                    )
        elif pool_opts:
            status = "version_mismatch"
            explain = _offered_message(pool_opts)
        elif any(requirement.accepts(option.key, option.version) for option in visible_excl):
            status = "provider_pending"
            names = ", ".join(sorted({option.entry_id for option in visible_excl}))
            explain = (
                f"provider(s) {names} for {requirement.name!r} are visible but not "
                f"active in this composition"
            )
        elif visible_excl:
            status = "version_mismatch"
            explain = _offered_message(visible_excl)
        elif any(
            option.entry_id != exclude_entry
            for option in all_options_by_name.get(requirement.name, ())
        ):
            status = "not_visible"
            explain = f"no provider for {requirement.name!r} is visible in scope {view.path}"
        elif any(key.name == requirement.name for key in provided_keys):
            status = "self_reference"
            explain = "the only declared provider is the consumer itself"
        else:
            status = "no_provider"
            explain = f"no provider for {requirement.name!r}"

        assessments = _assessments(
            all_options_by_name.get(requirement.name, ()),
            view=view,
            requirement=requirement,
            exclude_entry=exclude_entry,
            selected=selected,
            status=status,
        )
        origin: ProviderOrigin | None = None
        if selected is not None:
            origin = _origin(view, selected.scope)
        return RequirementResolution(
            requirement=requirement,
            status=status,
            provider_entry_id=None if selected is None else selected.entry_id,
            provider_instance_id=None if selected is None else selected.instance_id,
            provider_name=None if selected is None else selected.provider_name,
            provider_version=None if selected is None else str(selected.version),
            provider_key=None if selected is None else str(selected.key),
            explain=explain,
            consumer=consumer,
            consumer_kind=consumer_kind,
            provider_scope=None if selected is None else selected.scope,
            provider_origin=origin,
            selection_reason=selection_reason,
            assessments=assessments,
        )

    def _build_scope_plans(
        self,
        index: _ScopeIndex,
        candidates: tuple[PluginCandidate, ...],
        plan_by_entry: Mapping[str, PluginPlan],
        views: Mapping[str, _ScopeView],
        activation_order: tuple[str, ...],
        pending_ids: tuple[str, ...],
        selection: Mapping[str, str],
        all_options_by_name: Mapping[str, tuple[ProviderOption, ...]],
        cycle_members: frozenset[str],
    ) -> tuple[ScopePlan, ...]:
        by_scope: dict[str, list[str]] = {}
        for candidate in candidates:
            by_scope.setdefault(candidate.scope, []).append(candidate.entry_id)

        order_position = {entry_id: position for position, entry_id in enumerate(activation_order)}
        pending_set = set(pending_ids) | set(cycle_members)

        scope_plans: list[ScopePlan] = []
        for spec in index.ordered():
            entries = tuple(sorted(by_scope.get(spec.path, ())))
            order = tuple(
                sorted(
                    (entry for entry in entries if entry in order_position),
                    key=lambda entry: order_position[entry],
                )
            )
            pending = tuple(entry for entry in entries if entry in pending_set)
            view = views[spec.path]
            allowed = view.allowed
            providers: dict[str, tuple[str, ...]] = {}
            inherited: dict[str, tuple[str, ...]] = {}
            for name, options in sorted(view.visible.items()):
                local = tuple(
                    sorted({option.entry_id for option in options if option.scope == spec.path})
                )
                if local:
                    providers[name] = local
                non_local = tuple(
                    sorted({option.entry_id for option in options if option.scope != spec.path})
                )
                if non_local:
                    inherited[name] = non_local
            visible = {
                name: tuple(sorted({*providers.get(name, ()), *inherited.get(name, ())}))
                for name in sorted({*providers, *inherited})
            }
            requirements = tuple(
                self._resolve_one(
                    consumer=spec.path,
                    consumer_kind="scope",
                    requirement=requirement,
                    view=view,
                    selection=selection,
                    all_options_by_name=all_options_by_name,
                )
                for requirement in spec.requirements
            )
            provenance: list[RequirementResolution] = list(requirements)
            for entry in entries:
                plan = plan_by_entry.get(entry)
                if plan is not None:
                    provenance.extend(plan.requirements)
            provenance.sort(key=lambda item: (item.consumer, item.requirement.name))
            scope_plans.append(
                ScopePlan(
                    path=spec.path,
                    name=spec.name,
                    parent=spec.parent,
                    children=index.children(spec.path),
                    capabilities=None if allowed is None else tuple(sorted(allowed)),
                    tools=None if spec.tools is None else tuple(sorted(spec.tools)),
                    entries=entries,
                    order=order,
                    pending=pending,
                    providers=providers,
                    inherited=inherited,
                    visible=visible,
                    requirements=requirements,
                    provenance=tuple(provenance),
                    metadata=dict(spec.metadata),
                )
            )
        return tuple(scope_plans)


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
        for name, version in candidate.manifest.provided_contracts():
            option = ProviderOption(
                entry_id=candidate.entry_id,
                instance_id=candidate.instance_id,
                provider_name=candidate.manifest.name,
                key=CapabilityKey.from_version(name, version),
                version=Version(version),
                active=candidate.active,
                scope=candidate.scope,
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
            scope=candidate.scope,
        )
        for registration in candidate.registrations
    )


def _index_by_name(
    options_by_entry: Mapping[str, tuple[ProviderOption, ...]],
) -> dict[str, tuple[ProviderOption, ...]]:
    index: dict[str, list[ProviderOption]] = {}
    for entry_id in sorted(options_by_entry):
        for option in options_by_entry[entry_id]:
            index.setdefault(option.key.name, []).append(option)
    return {name: tuple(sorted(options, key=_option_order)) for name, options in index.items()}


def _build_views(
    index: _ScopeIndex,
    candidates: tuple[PluginCandidate, ...],
    options_by_entry: Mapping[str, tuple[ProviderOption, ...]],
    eligible: frozenset[str],
) -> dict[str, _ScopeView]:
    """Per-scope visible provider pools for one fixpoint iteration."""

    by_scope: dict[str, list[str]] = {}
    for candidate in candidates:
        by_scope.setdefault(candidate.scope, []).append(candidate.entry_id)

    views: dict[str, _ScopeView] = {}
    for path in index.paths():
        lineage = index.lineage(path)
        allowed = index.allowed(path)
        visible: dict[str, list[ProviderOption]] = {}
        for ancestor in lineage:
            for entry_id in by_scope.get(ancestor, ()):
                for option in options_by_entry.get(entry_id, ()):
                    if allowed is not None and option.key.name not in allowed:
                        continue
                    visible.setdefault(option.key.name, []).append(option)
        pool = {
            name: tuple(option for option in options if option.entry_id in eligible)
            for name, options in visible.items()
        }
        views[path] = _ScopeView(
            path=path,
            lineage=lineage,
            allowed=allowed,
            eligible=eligible,
            visible={
                name: tuple(sorted(options, key=_option_order)) for name, options in visible.items()
            },
            pool={
                name: tuple(sorted(options, key=_option_order)) for name, options in pool.items()
            },
        )
    return views


def _origin(view: _ScopeView, provider_scope: str) -> ProviderOrigin:
    if provider_scope == view.path:
        return "local"
    if provider_scope in view.lineage:
        return "inherited"
    return "unrelated"


def _assessments(
    options: tuple[ProviderOption, ...],
    *,
    view: _ScopeView,
    requirement: CapabilityRequirement,
    exclude_entry: str | None,
    selected: ProviderOption | None,
    status: RequirementStatus,
) -> tuple[ProviderAssessment, ...]:
    visible = set(view.visible.get(requirement.name, ()))
    records: list[ProviderAssessment] = []
    for option in sorted(options, key=_option_order):
        is_visible = option in visible
        relation = _origin(view, option.scope)
        origin: ProviderOrigin = relation
        rejection: str | None = None
        if option.entry_id == exclude_entry:
            rejection = "self_reference"
        elif not is_visible:
            rejection = "not_visible" if relation == "unrelated" else "capability_not_exposed"
        elif option.entry_id not in view.eligible:
            rejection = "provider_pending"
        elif not requirement.accepts(option.key, option.version):
            rejection = (
                "contract_mismatch"
                if requirement.key.api_version
                and option.key.api_version != requirement.key.api_version
                else "version_mismatch"
            )
        elif option == selected:
            rejection = None
        elif status == "ambiguous":
            rejection = "ambiguous"
        else:
            rejection = "not_selected"
        records.append(
            ProviderAssessment(
                provider_entry_id=option.entry_id,
                provider_instance_id=option.instance_id,
                provider_name=option.provider_name,
                version=str(option.version),
                scope=option.scope,
                origin=origin,
                visible=is_visible,
                eligible=(
                    is_visible
                    and option.entry_id in view.eligible
                    and option.entry_id != exclude_entry
                    and requirement.accepts(option.key, option.version)
                ),
                selected=selected is not None and option == selected,
                rejection=rejection,
            )
        )
    return tuple(records)


def _resolved_explain(option: ProviderOption, consumer_scope: str) -> str:
    base = f"provided by {option.entry_id} ({option.provider_name} {option.version})"
    if option.scope == consumer_scope:
        return base
    return f"{base} inherited from {option.scope}"


def _offered_message(options: tuple[ProviderOption, ...]) -> str:
    offered = ", ".join(f"{option.entry_id}@{option.version}" for option in options)
    return f"no provider satisfies the requirement; registered: {offered}"


def _option_order(option: ProviderOption) -> tuple[str, str, str, str, str]:
    return (
        option.provider_name,
        option.entry_id,
        option.instance_id or "",
        str(option.version),
        option.scope,
    )


def _preference(
    selection: Mapping[str, str], consumer: str, capability: str, scope: str
) -> str | None:
    """Most specific preference wins: consumer, then scope, then global."""

    scoped = selection.get(f"{consumer}:{capability}")
    if scoped is not None:
        return scoped
    by_scope = selection.get(f"scope:{scope}:{capability}")
    if by_scope is not None:
        return by_scope
    return selection.get(capability)


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
    index: _ScopeIndex,
) -> tuple[tuple[str, ...], ...]:
    """Strongly connected components of size > 1 in the declared dependency graph.

    Cycles are computed on the *observable* declared graph: a declared edge that
    scope visibility forbids (a sibling's or a descendant's provider) is not an
    edge, so a phantom cycle across isolated scopes cannot be reported.
    """

    nodes = tuple(candidate.entry_id for candidate in candidates)
    adjacency: dict[str, set[str]] = {entry: set() for entry in nodes}
    for candidate in candidates:
        for requirement in candidate.manifest.required_capabilities():
            for option in declared.get(requirement.name, ()):
                if option.entry_id == candidate.entry_id:
                    continue
                if not index.can_see(candidate.scope, option.scope, requirement.name):
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
