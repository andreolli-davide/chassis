"""The stable machine-readable planning contract.

`Harness.preview()` turns the existing planning, diffing, resolution, reuse, and
generation-impact machinery into one versioned operational document for CI,
deployment tooling, and operator review. The vocabulary is deliberately small
and closed: one action per affected entry plus a final publication action, each
carrying stable reason codes instead of parseable prose.

The contract is additive and versioned like every other exchanged Chassis
document (``PLAN_FORMAT_VERSION``): fields may be added, and consumers branch on
:class:`ActionKind` and :class:`ReasonCode`, never on rendered text. Sensitive
configuration never appears; actions name configuration *keys* only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from chassis.persistence.formats import PLAN_FORMAT_VERSION

__all__ = [
    "ActionKind",
    "Ambiguity",
    "GenerationImpact",
    "PlanAction",
    "PlanResult",
    "ReasonCode",
    "ValidationFailure",
]


class ActionKind(StrEnum):
    """What would happen to one entry — or to the generation as a whole."""

    ADD = "add"
    REMOVE = "remove"
    REPLACE = "replace"
    REUSE = "reuse"
    REBUILD = "rebuild"
    PUBLISH = "publish"
    REJECT = "reject"
    NOOP = "no-op"


class ReasonCode(StrEnum):
    """Stable reason codes. Values are the contract; never reword one.

    The change reasons mirror :class:`chassis.composition.ReuseReason` values,
    so impact analysis and planning name the same cause the same way.
    """

    ENTRY_ADDED = "entry_added"
    ENTRY_REMOVED = "entry_removed"
    IMPLEMENTATION_CHANGED = "implementation_changed"
    CONFIGURATION_CHANGED = "configuration_changed"
    SEMANTIC_IDENTITY_UNCHANGED = "semantic_identity_unchanged"
    DEPENDENCY_CHANGED = "dependency_changed"
    CAPABILITY_CONTRACT_CHANGED = "capability_contract_changed"
    SCOPE_VISIBILITY_CHANGED = "scope_visibility_changed"
    REQUIREMENT_UNSATISFIED = "requirement_unsatisfied"
    REQUIREMENT_AMBIGUOUS = "requirement_ambiguous"
    PENDING_DEPENDENCY = "pending_dependency"
    PLUGIN_CYCLE = "plugin_cycle"
    REVISION_CHANGED = "revision_changed"
    INSTANCE_INACTIVE = "instance_inactive"
    COMPOSITION_CHANGED = "composition_changed"
    COMPOSITION_UNCHANGED = "composition_unchanged"


class GenerationImpact(StrEnum):
    """Whether an action forces a new runtime generation."""

    NONE = "none"
    NEW_GENERATION = "new_generation"


#: ``SemanticIdentity.changed_inputs`` names -> planning reason codes.
INPUT_REASONS: Mapping[str, ReasonCode] = {
    "implementation": ReasonCode.IMPLEMENTATION_CHANGED,
    "contracts": ReasonCode.CAPABILITY_CONTRACT_CHANGED,
    "config": ReasonCode.CONFIGURATION_CHANGED,
    "scope": ReasonCode.SCOPE_VISIBILITY_CHANGED,
    "dependencies": ReasonCode.DEPENDENCY_CHANGED,
}


@dataclass(frozen=True, slots=True)
class ValidationFailure:
    """One reason a plan cannot proceed for an entry, as machine-readable data."""

    code: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Ambiguity:
    """A requirement several providers could satisfy, awaiting a preference."""

    consumer: str
    capability: str
    requirement: str
    candidates: tuple[str, ...]
    preference: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "consumer": self.consumer,
            "capability": self.capability,
            "requirement": self.requirement,
            "candidates": list(self.candidates),
            "preference": self.preference,
        }


@dataclass(frozen=True, slots=True)
class PlanAction:
    """One planned action with its stable reasons and expected effects."""

    action: ActionKind
    reasons: tuple[ReasonCode, ...]
    entry_id: str | None = None
    plugin: str | None = None
    scope: str = "/"
    config_keys: tuple[str, ...] = ()
    cause_capability: str | None = None
    cause_provider: str | None = None
    expected_reuse: bool = False
    instance_id: str | None = None
    generation_impact: GenerationImpact = GenerationImpact.NONE
    validation_failures: tuple[ValidationFailure, ...] = ()
    ambiguities: tuple[Ambiguity, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "reasons": [reason.value for reason in self.reasons],
            "entry_id": self.entry_id,
            "plugin": self.plugin,
            "scope": self.scope,
            "config_keys": list(self.config_keys),
            "cause_capability": self.cause_capability,
            "cause_provider": self.cause_provider,
            "expected_reuse": self.expected_reuse,
            "instance_id": self.instance_id,
            "generation_impact": self.generation_impact.value,
            "validation_failures": [failure.to_dict() for failure in self.validation_failures],
            "ambiguities": [ambiguity.to_dict() for ambiguity in self.ambiguities],
        }


@dataclass(frozen=True, slots=True)
class PlanResult:
    """What an apply would do, computed with zero mutation.

    ``actions`` are ordered by entry id with the final publication action last.
    ``would_publish`` states whether the apply would publish a new generation;
    ``pending`` and ``cycles`` carry the resolver's unresolved work; and
    ``preferences`` are the effective provider preferences the plan resolved
    with (keys are capability/consumer/scope selectors — values are provider
    entry ids, never configuration material).
    """

    chassis_version: str
    actions: tuple[PlanAction, ...]
    would_publish: bool
    current_generation_id: str | None = None
    pending: tuple[str, ...] = ()
    cycles: tuple[tuple[str, ...], ...] = ()
    preferences: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    format_version: int = PLAN_FORMAT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.preferences, MappingProxyType):
            object.__setattr__(self, "preferences", MappingProxyType(dict(self.preferences)))

    def action_for(self, entry_id: str) -> PlanAction | None:
        for action in self.actions:
            if action.entry_id == entry_id:
                return action
        return None

    @property
    def publication_action(self) -> PlanAction:
        """The trailing ``publish``/``no-op`` action."""

        return self.actions[-1]

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-compatible export (see ``PLAN_FORMAT_VERSION``)."""

        return {
            "format_version": self.format_version,
            "chassis_version": self.chassis_version,
            "would_publish": self.would_publish,
            "current_generation_id": self.current_generation_id,
            "pending": list(self.pending),
            "cycles": [list(cycle) for cycle in self.cycles],
            "preferences": dict(sorted(self.preferences.items())),
            "actions": [action.to_dict() for action in self.actions],
        }
