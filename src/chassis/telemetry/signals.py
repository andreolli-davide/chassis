"""The stable operational signal contract.

Every span and event Chassis emits is named and shaped here. The contract is
backend-independent: it is tested against the in-repo recording backend, and
every adapter (LangSmith, OpenTelemetry, custom) transports these signals
without renaming or reshaping them.

Rules the contract enforces (guarantee G27):

- **Names are stable.** A signal name and its required attribute keys are API;
  they may gain optional attributes but never lose required ones.
- **Correlation is explicit.** The correlation fields — run id, thread id,
  generation id, snapshot digest, agent and revision, plugin entry and instance
  id, tool registration id, scope — are attached at the boundary that owns the
  fact, never invented downstream.
- **Cardinality is bounded.** Attribute values are scalars or short bounded
  sequences of scalars. Raw payloads — requests, responses, messages, prompt
  bodies, configuration material — are forbidden in signals; identity values
  (run ids, key digests) are high-cardinality and belong on span attributes,
  never on metric labels.
- **No secrets.** The redaction boundary scrubs every attribute before any
  backend sees it; sensitive-key values and learned secret values cannot appear.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "ATTRIBUTE_ITEMS_LIMIT",
    "ATTRIBUTE_STRING_LIMIT",
    "CORRELATION_FIELDS",
    "SIGNALS",
    "SignalKind",
    "SignalSpec",
    "validate_signal",
]

#: Longest string attribute value; longer values are payloads, not signals.
ATTRIBUTE_STRING_LIMIT = 512

#: Most items in a sequence attribute value.
ATTRIBUTE_ITEMS_LIMIT = 32

#: Values that are always acceptable as attributes.
_SCALAR = (str, int, float, bool, type(None))


class SignalKind(StrEnum):
    """Whether a signal has duration."""

    SPAN = "span"
    EVENT = "event"


@dataclass(frozen=True, slots=True)
class SignalSpec:
    """One stable signal: its name, shape, and role in correlation."""

    name: str
    kind: SignalKind
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    description: str = ""

    def violations(self, attributes: Mapping[str, Any]) -> tuple[str, ...]:
        """Contract violations for one emission of this signal."""

        problems: list[str] = []
        for key in self.required:
            if key not in attributes:
                problems.append(f"{self.name}: missing required attribute {key!r}")
        for key, value in attributes.items():
            if isinstance(value, _SCALAR):
                if isinstance(value, str) and len(value) > ATTRIBUTE_STRING_LIMIT:
                    problems.append(
                        f"{self.name}: attribute {key!r} exceeds {ATTRIBUTE_STRING_LIMIT} chars"
                    )
                continue
            if isinstance(value, (list, tuple)):
                if len(value) > ATTRIBUTE_ITEMS_LIMIT:
                    problems.append(
                        f"{self.name}: attribute {key!r} exceeds {ATTRIBUTE_ITEMS_LIMIT} items"
                    )
                if not all(isinstance(item, _SCALAR) for item in value):
                    problems.append(f"{self.name}: attribute {key!r} holds non-scalar items")
                continue
            problems.append(
                f"{self.name}: attribute {key!r} is an unbounded payload ({type(value).__name__})"
            )
        return tuple(problems)


#: Correlation fields, named at the boundary that owns the fact.
CORRELATION_FIELDS: tuple[str, ...] = (
    "run_id",
    "thread_id",
    "generation_id",
    "snapshot_digest",
    "agent",
    "agent_revision",
    "entry_id",
    "instance_id",
    "registration_id",
    "scope_id",
)


def _spec(
    name: str, kind: SignalKind, required: Sequence[str], optional: Sequence[str] = (), **kw: str
) -> SignalSpec:
    spec = SignalSpec(
        name=name,
        kind=kind,
        required=tuple(required),
        optional=tuple(optional),
        description=kw.get("description", ""),
    )
    return spec


#: Every signal Chassis emits. Additive optional attributes are compatible
#: changes; a missing required attribute on an emission is a defect.
SIGNALS: Mapping[str, SignalSpec] = {
    spec.name: spec
    for spec in (
        _spec(
            "harness.reconcile",
            SignalKind.SPAN,
            ("harness",),
            ("desired", "generation_id", "mounted", "reused", "disposed", "failures", "rebuilt"),
            description="One reconciliation, from resolution to publication or no-op.",
        ),
        _spec(
            "harness.shutdown",
            SignalKind.SPAN,
            ("harness",),
            description="One shutdown run, from drain to disposal.",
        ),
        _spec(
            "plugin.mount",
            SignalKind.SPAN,
            ("plugin", "entry_id"),
            ("revision", "instance_id"),
            description="Plugin setup for one candidate instance.",
        ),
        _spec(
            "plugin.unmount",
            SignalKind.SPAN,
            ("plugin", "entry_id", "instance_id"),
            ("state",),
            description="Plugin teardown and scope close for one instance.",
        ),
        _spec(
            "tool.execute",
            SignalKind.SPAN,
            ("tool", "generation_id"),
            ("owner", "idempotent", "replayed", "registration_id", "run_id", "tool_call_id"),
            description="One tool call through the live boundary (tagged replayed when served).",
        ),
        _spec(
            "agent.run",
            SignalKind.SPAN,
            ("agent", "agent_revision", "generation_id"),
            (
                "thread_id",
                "chassis_version",
                "snapshot_digest",
                "plugin_graph_hash",
                "graph_definition_hash",
                "tool_schema_hash",
                "run_id",
            ),
            description="One agent invocation or stream, with snapshot attribution.",
        ),
        _spec(
            "dependency.resolve",
            SignalKind.EVENT,
            ("eligible", "pending", "cycles", "edges", "scopes"),
            description="One dependency resolution outcome.",
        ),
        _spec(
            "generation.build",
            SignalKind.EVENT,
            ("plugins", "mounted"),
            description="A candidate generation was assembled.",
        ),
        _spec(
            "generation.publish",
            SignalKind.EVENT,
            ("generation_id", "sequence", "previous", "plugins"),
            description="A generation became current.",
        ),
        _spec(
            "generation.acquire",
            SignalKind.EVENT,
            ("generation_id", "sequence"),
            ("run_id",),
            description="A run leased the current generation.",
        ),
        _spec(
            "generation.release",
            SignalKind.EVENT,
            ("generation_id", "leases"),
            ("run_id",),
            description="A run released its lease.",
        ),
        _spec(
            "generation.draining",
            SignalKind.EVENT,
            ("generation_id", "successor", "leases"),
            description="The previous generation began draining.",
        ),
        _spec(
            "generation.retired",
            SignalKind.EVENT,
            ("generation_id",),
            ("leases",),
            description="A draining generation retired.",
        ),
        _spec(
            "generation.impact",
            SignalKind.EVENT,
            (),
            ("added", "removed", "reused", "rebuilt", "rewired", "unchanged"),
            description="Reuse/rebuild decision counts for one publication.",
        ),
        _spec(
            "policy.decision",
            SignalKind.EVENT,
            ("tool", "allowed"),
            ("permission", "permissions", "reason"),
            description="One authorization decision for a tool call.",
        ),
        _spec(
            "budget.exhausted",
            SignalKind.EVENT,
            ("tool", "dimension"),
            ("run_id", "generation_id"),
            description="A budget dimension was exhausted at the tool boundary.",
        ),
        _spec(
            "replay.hit",
            SignalKind.EVENT,
            ("kind",),
            ("tool", "key", "run_id", "generation_id"),
            description="A recorded boundary answered the operation.",
        ),
        _spec(
            "replay.miss",
            SignalKind.EVENT,
            ("kind",),
            ("tool", "fallback", "key"),
            description="No record exists for the boundary key.",
        ),
        _spec(
            "replay.exhausted",
            SignalKind.EVENT,
            ("kind",),
            ("tool", "key"),
            description="The boundary key's records were consumed.",
        ),
        _spec(
            "graph.compile",
            SignalKind.EVENT,
            ("agent", "definition_version"),
            ("cache_key", "tools"),
            description="A graph was compiled and cached.",
        ),
        _spec(
            "graph.cache",
            SignalKind.EVENT,
            ("result",),
            ("agent", "definition_version", "cache_key"),
            description="One cache lookup or eviction (result: hit, miss, evict).",
        ),
        _spec(
            "graph.cache.invalidate",
            SignalKind.EVENT,
            ("agent", "entries"),
            description="Cached graphs were invalidated.",
        ),
        _spec(
            "hook.failure",
            SignalKind.EVENT,
            ("event", "error_type"),
            ("description",),
            description="A hook handler failed and was recorded, not raised.",
        ),
        _spec(
            "cleanup.failure",
            SignalKind.EVENT,
            ("error_type",),
            ("entry_id", "scope", "description"),
            description="A disposer or teardown step failed and was aggregated.",
        ),
        _spec(
            "telemetry.failure",
            SignalKind.EVENT,
            ("operation", "signal"),
            ("backend",),
            description="A telemetry backend failed and was contained.",
        ),
    )
}


def validate_signal(name: str, attributes: Mapping[str, Any]) -> tuple[str, ...]:
    """Contract violations for one emission — empty when the signal conforms."""

    spec = SIGNALS.get(name)
    if spec is None:
        return (f"unknown signal {name!r}",)
    return spec.violations(attributes)
