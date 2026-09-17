"""Semantic identity and incremental impact analysis for composition nodes.

A runtime generation publishes an immutable composition, but composition itself
changes over time. 0.4 makes that change *incremental*: a node whose semantic
inputs are unchanged may be reused from the generation it came from, and a node
whose inputs changed must be rebuilt. This module defines what "semantic inputs"
means and how affected nodes are found.

Semantic identity vs Python identity
------------------------------------

A :class:`SemanticIdentity` describes the *behaviour* of one composition node --
a mounted :class:`~chassis.plugins.lifecycle.PluginInstance` keyed by its entry id
-- not the Python object that happens to implement it. Two identities compare
equal only when every input that can affect observable behaviour is equal:

- implementation fingerprint: the plugin implementation (manifest identity plus
  the concrete class that implements it);
- contract fingerprint: the capability contracts it declares (provides,
  requires, optional, permissions, config version);
- config fingerprint: the effective configuration, *including* secret-bearing
  values, because a credential change must force a rebuild. This fingerprint is
  opaque and is never emitted: it exists only to decide reuse;
- scope path: moving an entry to another scope changes visibility;
- dependency bindings: the provider each requirement resolved to, identified by
  both the provider *entry* (semantic) and the provider *instance* (physical).

Physical vs semantic equality
-----------------------------

A binding records both the provider's behavioural digest (transitive: a provider
whose own resolution changed has a new digest) and the provider's runtime
instance id. Reuse requires both to match, because a reused consumer keeps the
capability objects it captured during ``setup``. When the provider's runtime
instance changed, the consumer must be rebuilt even if the provider's behaviour
is unchanged -- its registration would otherwise point at a disposed instance.

``physically reused`` and ``semantically unchanged`` are therefore separate
facts, and both are reported.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from chassis.persistence.hashing import stable_hash
from chassis.secrets.redaction import SecretRedactor, redact_config

if TYPE_CHECKING:
    from chassis.plugins.lifecycle import PluginInstance
    from chassis.plugins.manifest import PluginManifest
    from chassis.plugins.resolver import RequirementResolution

__all__ = [
    "DependencyBinding",
    "ImpactAnalysis",
    "NodeImpact",
    "NodeObservation",
    "ReuseDecision",
    "ReuseReason",
    "SemanticIdentity",
    "analyse_impact",
    "build_semantic_identity",
    "contract_fingerprint",
    "implementation_fingerprint",
    "observations_from",
]


class ReuseDecision(StrEnum):
    """Outcome of comparing one composition node across two generations.

    ``UNCHANGED`` and ``REUSED`` are deliberately distinct: ``UNCHANGED`` means
    the node is semantically identical but was not (or could not be) physically
    reused, while ``REUSED`` is only ever reported when the exact same runtime
    instance was retained.
    """

    UNCHANGED = "unchanged"
    REUSED = "reused"
    REBUILT = "rebuilt"
    REWIRED = "rewired"
    ADDED = "added"
    REMOVED = "removed"


class ReuseReason(StrEnum):
    """Why a node could not be reused. Small and explicit by design."""

    CONFIG_CHANGED = "config_changed"
    IMPLEMENTATION_CHANGED = "implementation_changed"
    DEPENDENCY_CHANGED = "dependency_changed"
    SCOPE_VISIBILITY_CHANGED = "scope_visibility_changed"
    PROVIDER_SELECTION_CHANGED = "provider_selection_changed"
    CAPABILITY_CONTRACT_CHANGED = "capability_contract_changed"
    PREFERENCE_CHANGED = "preference_changed"


#: Input names :meth:`SemanticIdentity.changed_inputs` reports. They map to a
#: :class:`ReuseReason` in :func:`analyse_impact`.
_IMPLEMENTATION = "implementation"
_CONTRACTS = "contracts"
_CONFIG = "config"
_SCOPE = "scope"
_DEPENDENCIES = "dependencies"

_REASON_FOR_INPUT: Mapping[str, ReuseReason] = {
    _IMPLEMENTATION: ReuseReason.IMPLEMENTATION_CHANGED,
    _CONTRACTS: ReuseReason.CAPABILITY_CONTRACT_CHANGED,
    _CONFIG: ReuseReason.CONFIG_CHANGED,
    _SCOPE: ReuseReason.SCOPE_VISIBILITY_CHANGED,
    _DEPENDENCIES: ReuseReason.DEPENDENCY_CHANGED,
}


def implementation_fingerprint(
    manifest: PluginManifest, implementation_hint: str | None = None
) -> str:
    """Identity of the plugin implementation behind an entry.

    The manifest identity (``name@version``) is the primary signal. The
    implementation hint disambiguates two classes that declare the same manifest,
    which a replacement install can produce. An author-declared
    ``implementation_revision`` disambiguates two builds that share both, and is
    absent from the payload when unset so existing identities are unchanged.
    """

    payload: dict[str, object] = {
        "identity": manifest.identity,
        "implementation": implementation_hint or "",
    }
    if manifest.implementation_revision is not None:
        payload["implementation_revision"] = manifest.implementation_revision
    return stable_hash(payload)


def contract_fingerprint(manifest: PluginManifest) -> str:
    """Identity of the capability contracts an entry declares."""

    return stable_hash(
        {
            "provides": dict(sorted(manifest.provides.items())),
            "requires": dict(sorted(manifest.requires.items())),
            "optional": dict(sorted(manifest.optional.items())),
            "permissions": list(manifest.permissions),
            "config_version": manifest.config_version,
        }
    )


@dataclass(frozen=True, slots=True)
class DependencyBinding:
    """How one requirement of a node resolved in one composition.

    ``provider_identity`` is the provider's behavioural digest: it changes when
    the provider's implementation, configuration, scope, or own dependencies
    change, which is what makes impact analysis transitive. ``provider_instance_id``
    is physical and participates in reuse; both are compared by
    :meth:`SemanticIdentity` equality.

    ``provider_display`` is the provider's displayable semantic id: it exists only
    so the node's own ``semantic_id`` stays free of secret-derived digests, and it
    never participates in equality or reuse.

    ``preference`` records which explicit preference (if any) selected the
    provider. It is explanatory only: it never participates in reuse, because a
    preference change that selects the same provider does not change behaviour.
    """

    capability: str
    consumer: str
    provider_entry_id: str | None = None
    provider_instance_id: str | None = None
    provider_identity: str | None = field(default=None, repr=False)
    provider_display: str | None = field(default=None, compare=False, repr=False)
    status: str = "no_provider"
    preference: str | None = field(default=None, compare=False, repr=False)

    @property
    def satisfied(self) -> bool:
        return self.provider_entry_id is not None

    def reuse_key(self) -> tuple[object, ...]:
        """Physical reuse key: the resolved provider *instance* matters."""

        return (
            self.capability,
            self.provider_entry_id,
            self.provider_instance_id,
            self.status,
        )

    def semantic_key(self) -> tuple[object, ...]:
        """Behavioural key: same provider behaviour, instance-independent."""

        return (
            self.capability,
            self.provider_entry_id,
            self.provider_identity,
            self.status,
        )

    def display_key(self) -> tuple[object, ...]:
        """Non-secret key used for the displayable semantic id."""

        return (
            self.capability,
            self.provider_entry_id,
            self.provider_display,
            self.status,
            self.preference,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "consumer": self.consumer,
            "provider_entry_id": self.provider_entry_id,
            "provider_instance_id": self.provider_instance_id,
            "status": self.status,
            "preference": self.preference,
        }


@dataclass(frozen=True, slots=True)
class SemanticIdentity:
    """Behavioural identity of one composition node.

    Equality (and therefore reuse eligibility, together with the desired
    revision) means *safe to reuse according to Chassis lifecycle guarantees*.
    The config and dependency fingerprints are deliberately excluded from
    :meth:`to_dict`; they are opaque comparison values, and the config fingerprint
    is derived from unredacted configuration so that a credential change forces a
    rebuild.
    """

    entry_id: str
    kind: str
    scope_path: str
    implementation_fingerprint: str
    contract_fingerprint: str
    config_fingerprint: str = field(repr=False)
    dependency_fingerprint: str = field(repr=False)
    config_display_fingerprint: str = field(default="", repr=False)
    bindings: tuple[DependencyBinding, ...] = ()

    # ------------------------------------------------------------------ digests

    def identity_digest(self) -> str:
        """Transitive behavioural digest, used as a provider's identity.

        Never emitted: it folds in the config fingerprint.
        """

        return stable_hash(
            {
                "kind": self.kind,
                "implementation": self.implementation_fingerprint,
                "contracts": self.contract_fingerprint,
                "config": self.config_fingerprint,
                "scope": self.scope_path,
                "dependencies": self.dependency_fingerprint,
            }
        )

    @property
    def semantic_id(self) -> str:
        """Displayable identity, built from non-secret structure only."""

        return stable_hash(
            {
                "entry_id": self.entry_id,
                "kind": self.kind,
                "scope_path": self.scope_path,
                "implementation": self.implementation_fingerprint,
                "contracts": self.contract_fingerprint,
                "config": self.config_display_fingerprint,
                "bindings": [list(binding.display_key()) for binding in self.bindings],
            }
        )

    def reuse_key(self) -> tuple[object, ...]:
        return (
            self.entry_id,
            self.kind,
            self.scope_path,
            self.implementation_fingerprint,
            self.contract_fingerprint,
            self.config_fingerprint,
            self.dependency_fingerprint,
            tuple(binding.reuse_key() for binding in self.bindings),
        )

    def semantic_key(self) -> tuple[object, ...]:
        return (
            self.entry_id,
            self.kind,
            self.scope_path,
            self.implementation_fingerprint,
            self.contract_fingerprint,
            self.config_fingerprint,
            tuple(binding.semantic_key() for binding in self.bindings),
        )

    def semantically_equal(self, other: SemanticIdentity) -> bool:
        """Whether reuse cannot change observable behaviour.

        Ignores the provider *instance* ids: two nodes are semantically equal when
        they resolve to behaviourally identical providers, even if those providers
        were materialised separately.
        """

        return self.semantic_key() == other.semantic_key()

    def changed_inputs(self, other: SemanticIdentity) -> tuple[str, ...]:
        """Named semantic inputs that differ from ``other``, in stable order."""

        changed: list[str] = []
        if self.implementation_fingerprint != other.implementation_fingerprint:
            changed.append(_IMPLEMENTATION)
        if self.contract_fingerprint != other.contract_fingerprint:
            changed.append(_CONTRACTS)
        if self.config_fingerprint != other.config_fingerprint:
            changed.append(_CONFIG)
        if self.scope_path != other.scope_path:
            changed.append(_SCOPE)
        if self.dependency_fingerprint != other.dependency_fingerprint:
            changed.append(_DEPENDENCIES)
        return tuple(changed)

    # ------------------------------------------------------------------ output

    def to_dict(self) -> dict[str, object]:
        return {
            "entry_id": self.entry_id,
            "kind": self.kind,
            "scope_path": self.scope_path,
            "semantic_id": self.semantic_id,
            "implementation_fingerprint": self.implementation_fingerprint,
            "contract_fingerprint": self.contract_fingerprint,
            "bindings": [binding.to_dict() for binding in self.bindings],
        }


def build_semantic_identity(
    *,
    entry_id: str,
    scope_path: str,
    manifest: PluginManifest,
    config: Mapping[str, object],
    resolutions: Sequence[RequirementResolution],
    provider_instance: Callable[[str], str | None],
    provider_identity: Callable[[str], str | None],
    provider_display: Callable[[str], str | None],
    preference: Callable[[str, str, str], str | None],
    implementation_hint: str | None = None,
    kind: str = "plugin",
    redactor: SecretRedactor | None = None,
) -> SemanticIdentity:
    """Build the semantic identity of one node from authoritative runtime state.

    ``provider_instance`` and ``provider_identity`` are resolved through the
    control plane rather than trusting the plan's cached instance ids: the plan is
    computed before mounting, so its ids can name an outgoing instance. Providers
    are resolved (and therefore given identities) before their consumers, so the
    functions always answer for an already-decided node.
    """

    from chassis.config.reconcile import config_fingerprint

    bindings: list[DependencyBinding] = []
    for resolution in resolutions:
        capability = resolution.requirement.name
        provider = resolution.provider_entry_id
        bindings.append(
            DependencyBinding(
                capability=capability,
                consumer=entry_id,
                provider_entry_id=provider,
                provider_instance_id=None if provider is None else provider_instance(provider),
                provider_identity=None if provider is None else provider_identity(provider),
                provider_display=None if provider is None else provider_display(provider),
                status=resolution.status,
                preference=_applicable_preference(preference, entry_id, capability, scope_path),
            )
        )
    bindings.sort(key=lambda binding: binding.capability)
    effective_redactor = redactor if redactor is not None else SecretRedactor()
    return SemanticIdentity(
        entry_id=entry_id,
        kind=kind,
        scope_path=scope_path,
        implementation_fingerprint=implementation_fingerprint(manifest, implementation_hint),
        contract_fingerprint=contract_fingerprint(manifest),
        config_fingerprint=config_fingerprint(plugin=manifest.name, config=config),
        config_display_fingerprint=stable_hash(redact_config(dict(config), effective_redactor)),
        dependency_fingerprint=stable_hash([list(binding.semantic_key()) for binding in bindings]),
        bindings=tuple(bindings),
    )


def _applicable_preference(
    lookup: Callable[[str, str, str], str | None],
    consumer: str,
    capability: str,
    scope: str,
) -> str | None:
    return lookup(consumer, capability, scope)


# ---------------------------------------------------------------------- impact


@dataclass(frozen=True, slots=True)
class NodeObservation:
    """One node as observed in one generation, for impact analysis."""

    entry_id: str
    kind: str
    scope_path: str
    instance_id: str
    identity: SemanticIdentity | None
    scope_view: tuple[str, ...] | None = None


def observations_from(
    instances: Sequence[PluginInstance],
    *,
    scope_view: Callable[[str], tuple[str, ...] | None] | None = None,
) -> dict[str, NodeObservation]:
    """Index a generation's mounted nodes by entry id."""

    observed: dict[str, NodeObservation] = {}
    for instance in instances:
        identity = instance.semantic_identity
        scope_path = "" if identity is None else identity.scope_path
        observed[instance.entry_id] = NodeObservation(
            entry_id=instance.entry_id,
            kind="plugin",
            scope_path=scope_path,
            instance_id=instance.instance_id,
            identity=identity,
            scope_view=None if scope_view is None else scope_view(scope_path),
        )
    return observed


@dataclass(frozen=True, slots=True)
class NodeImpact:
    """What happened to one node between two generations."""

    entry_id: str
    kind: str
    scope_path: str
    decision: str
    reasons: tuple[str, ...]
    changed_inputs: tuple[str, ...]
    dependency_changes: tuple[str, ...]
    old_semantic_id: str | None
    new_semantic_id: str | None
    old_instance_id: str | None
    new_instance_id: str | None
    shared_instance_id: str | None
    semantically_unchanged: bool
    physically_reused: bool

    @property
    def reused(self) -> bool:
        return self.decision == ReuseDecision.REUSED

    @property
    def rebuilt(self) -> bool:
        return self.decision == ReuseDecision.REBUILT

    @property
    def rewired(self) -> bool:
        return self.decision == ReuseDecision.REWIRED

    def to_dict(self) -> dict[str, object]:
        return {
            "entry_id": self.entry_id,
            "kind": self.kind,
            "scope_path": self.scope_path,
            "decision": self.decision,
            "reasons": list(self.reasons),
            "changed_inputs": list(self.changed_inputs),
            "dependency_changes": list(self.dependency_changes),
            "old_semantic_id": self.old_semantic_id,
            "new_semantic_id": self.new_semantic_id,
            "old_instance_id": self.old_instance_id,
            "new_instance_id": self.new_instance_id,
            "shared_instance_id": self.shared_instance_id,
            "semantically_unchanged": self.semantically_unchanged,
            "physically_reused": self.physically_reused,
        }

    def to_text(self) -> str:
        lines = [f"{self.entry_id} ({self.kind})"]
        lines.append(f"  decision: {self.decision}")
        if self.scope_path:
            lines.append(f"  scope: {self.scope_path}")
        if self.reasons:
            lines.append(f"  reasons: {', '.join(self.reasons)}")
        if self.dependency_changes:
            lines.append(f"  dependencies changed: {', '.join(self.dependency_changes)}")
        if self.shared_instance_id is not None:
            lines.append(f"  shared instance: {self.shared_instance_id}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ImpactAnalysis:
    """Deterministic, inspectable result of comparing two compositions."""

    old_generation_id: str | None
    new_generation_id: str | None
    nodes: tuple[NodeImpact, ...]
    include_unchanged: bool = True

    def get(self, entry_id: str) -> NodeImpact | None:
        for node in self.nodes:
            if node.entry_id == entry_id:
                return node
        return None

    def by_decision(self, decision: ReuseDecision | str) -> tuple[NodeImpact, ...]:
        value = decision.value if isinstance(decision, ReuseDecision) else decision
        return tuple(node for node in self.nodes if node.decision == value)

    @property
    def reused(self) -> tuple[NodeImpact, ...]:
        return self.by_decision(ReuseDecision.REUSED)

    @property
    def rebuilt(self) -> tuple[NodeImpact, ...]:
        return self.by_decision(ReuseDecision.REBUILT)

    @property
    def rewired(self) -> tuple[NodeImpact, ...]:
        return self.by_decision(ReuseDecision.REWIRED)

    @property
    def added(self) -> tuple[NodeImpact, ...]:
        return self.by_decision(ReuseDecision.ADDED)

    @property
    def removed(self) -> tuple[NodeImpact, ...]:
        return self.by_decision(ReuseDecision.REMOVED)

    @property
    def unchanged(self) -> tuple[NodeImpact, ...]:
        return self.by_decision(ReuseDecision.UNCHANGED)

    def counts(self) -> dict[str, int]:
        """Decision counts, so an efficiency claim is a deterministic number."""

        counted: dict[str, int] = {}
        for node in self.nodes:
            counted[node.decision] = counted.get(node.decision, 0) + 1
        return counted

    def to_dict(self) -> dict[str, object]:
        return {
            "old_generation_id": self.old_generation_id,
            "new_generation_id": self.new_generation_id,
            "counts": self.counts(),
            "nodes": [node.to_dict() for node in self.nodes],
        }

    def to_text(self) -> str:
        header = (
            f"{self.old_generation_id or '(desired)'} -> {self.new_generation_id or '(candidate)'}"
        )
        lines = [header, ""]
        for decision in (
            ReuseDecision.REUSED,
            ReuseDecision.REBUILT,
            ReuseDecision.REWIRED,
            ReuseDecision.ADDED,
            ReuseDecision.REMOVED,
            ReuseDecision.UNCHANGED,
        ):
            group = self.by_decision(decision)
            if not group:
                continue
            lines.append(decision.value.upper())
            for node in group:
                lines.append(f"  {node.entry_id}")
                if node.reasons:
                    lines.append(f"    reason: {', '.join(node.reasons)}")
                elif node.dependency_changes:
                    lines.append(f"    reason: {', '.join(node.dependency_changes)} changed")
            lines.append("")
        if len(lines) == 2:
            lines.append("(no composition nodes)")
        return "\n".join(lines).rstrip()


def analyse_impact(
    old: Mapping[str, NodeObservation],
    new: Mapping[str, NodeObservation],
    *,
    old_generation_id: str | None = None,
    new_generation_id: str | None = None,
    include_unchanged: bool = True,
) -> ImpactAnalysis:
    """Compare two observed compositions and decide every node's fate.

    The comparison follows actual dependency bindings, not scope membership, so a
    change in one tenant's subtree never rebuilds a sibling's identical subtree.
    """

    nodes: list[NodeImpact] = []
    for entry_id in sorted({*old, *new}):
        before = old.get(entry_id)
        after = new.get(entry_id)
        if before is None and after is None:  # pragma: no cover - defensive
            continue
        if before is None:
            assert after is not None
            nodes.append(_single(entry_id, after, ReuseDecision.ADDED))
            continue
        if after is None:
            nodes.append(_single(entry_id, before, ReuseDecision.REMOVED))
            continue
        nodes.append(_compare(before, after))
    if not include_unchanged:
        nodes = [node for node in nodes if node.decision != ReuseDecision.UNCHANGED]
    return ImpactAnalysis(
        old_generation_id=old_generation_id,
        new_generation_id=new_generation_id,
        nodes=tuple(nodes),
        include_unchanged=include_unchanged,
    )


def _single(entry_id: str, observation: NodeObservation, decision: ReuseDecision) -> NodeImpact:
    identity = observation.identity
    semantic_id = None if identity is None else identity.semantic_id
    added = decision is ReuseDecision.ADDED
    removed = decision is ReuseDecision.REMOVED
    return NodeImpact(
        entry_id=entry_id,
        kind=observation.kind,
        scope_path=observation.scope_path,
        decision=decision.value,
        reasons=(),
        changed_inputs=(),
        dependency_changes=(),
        old_semantic_id=None if added else semantic_id,
        new_semantic_id=None if removed else semantic_id,
        old_instance_id=None if added else observation.instance_id,
        new_instance_id=None if decision is ReuseDecision.REMOVED else observation.instance_id,
        shared_instance_id=None,
        semantically_unchanged=False,
        physically_reused=False,
    )


def _compare(before: NodeObservation, after: NodeObservation) -> NodeImpact:
    physically_reused = before.instance_id == after.instance_id
    old_identity = before.identity
    new_identity = after.identity
    if old_identity is not None and new_identity is not None:
        semantically_unchanged = old_identity.semantically_equal(new_identity)
        inputs: tuple[str, ...] = old_identity.changed_inputs(new_identity)
    else:
        semantically_unchanged = False
        inputs = (_IMPLEMENTATION,)
    if physically_reused and semantically_unchanged:
        decision = ReuseDecision.REUSED
        reasons: tuple[str, ...] = ()
        changed_inputs: tuple[str, ...] = ()
        dependency_changes: tuple[str, ...] = ()
    elif semantically_unchanged:
        decision = ReuseDecision.UNCHANGED
        reasons = ()
        changed_inputs = ()
        dependency_changes = ()
    else:
        changed_inputs = inputs
        reasons, dependency_changes = _reasons(before, after, inputs)
        own_changed = any(item in inputs for item in (_IMPLEMENTATION, _CONTRACTS, _CONFIG, _SCOPE))
        decision = (
            ReuseDecision.REWIRED
            if _DEPENDENCIES in inputs and not own_changed
            else ReuseDecision.REBUILT
        )
    return NodeImpact(
        entry_id=after.entry_id,
        kind=after.kind,
        scope_path=after.scope_path or before.scope_path,
        decision=decision.value,
        reasons=reasons,
        changed_inputs=changed_inputs,
        dependency_changes=dependency_changes,
        old_semantic_id=None if before.identity is None else before.identity.semantic_id,
        new_semantic_id=None if after.identity is None else after.identity.semantic_id,
        old_instance_id=before.instance_id,
        new_instance_id=after.instance_id,
        shared_instance_id=after.instance_id if physically_reused else None,
        semantically_unchanged=semantically_unchanged,
        physically_reused=physically_reused,
    )


def _reasons(
    before: NodeObservation, after: NodeObservation, inputs: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    reasons: list[ReuseReason] = []
    dependency_changes: list[str] = []
    for item in inputs:
        reason = _REASON_FOR_INPUT.get(item)
        if reason is not None:
            reasons.append(reason)
    if _DEPENDENCIES in inputs and before.identity is not None and after.identity is not None:
        selection, preference, changed = _binding_changes(before.identity, after.identity)
        dependency_changes = changed
        if selection:
            reasons.append(ReuseReason.PROVIDER_SELECTION_CHANGED)
        if preference:
            reasons.append(ReuseReason.PREFERENCE_CHANGED)
    if (
        before.scope_view != after.scope_view
        and _DEPENDENCIES in inputs
        and ReuseReason.SCOPE_VISIBILITY_CHANGED not in reasons
    ):
        reasons.append(ReuseReason.SCOPE_VISIBILITY_CHANGED)
    seen: list[str] = []
    for reason in reasons:
        if reason.value not in seen:
            seen.append(reason.value)
    return tuple(seen), tuple(sorted(set(dependency_changes)))


def _binding_changes(
    before: SemanticIdentity, after: SemanticIdentity
) -> tuple[bool, bool, list[str]]:
    old = {binding.capability: binding for binding in before.bindings}
    new = {binding.capability: binding for binding in after.bindings}
    selection = False
    preference = False
    changed: list[str] = []
    for capability in sorted({*old, *new}):
        previous = old.get(capability)
        current = new.get(capability)
        if previous == current:
            continue
        if previous is None or current is None:
            selection = True
        else:
            if (
                previous.provider_entry_id != current.provider_entry_id
                or previous.provider_instance_id != current.provider_instance_id
            ):
                selection = True
            if previous.preference != current.preference:
                preference = True
        for binding in (previous, current):
            if binding is not None and binding.provider_entry_id is not None:
                changed.append(binding.provider_entry_id)
    return selection, preference, changed
