"""First-class, immutable agent composition descriptions.

An :class:`AgentSpec` answers one question: *what runtime composition does this
agent see?* It names a logical agent, pins an author-declared revision, and
declares the composition its scope materializes -- capability visibility,
requirements, plugin contributions, tool visibility, and the runtime it binds to.

It deliberately answers nothing else. It does not build or run a graph, does not
decide how an agent thinks, and grants no user or organization authority. The
execution contract lives elsewhere: LangGraph's ``AgentDefinition`` remains *how a
specific engine builds and runs a graph*, and an ``AgentSpec`` only references such
a runtime by a logical ``runtime_ref``. This keeps the model engine-neutral and the
core importable without LangGraph.

Immutability
------------

A spec is frozen and every container it holds is frozen recursively (see
:func:`~chassis.core.collections.freeze`), so a control-plane authoring object can
never alias mutable state into a published revision. A revision, once published,
is never mutated in place: a composition-affecting change produces a new revision
(see :class:`~chassis.agents.AgentRevision` and the agent registry).

Composition, not authorization
------------------------------

``capabilities`` and ``tools`` describe *visibility* inside the agent's scope,
using the same narrowing rule a composition scope already applies. They are not
IAM: a tool visible to an agent is not a tool authorized for the current user or
action. External authorization stays the responsibility of the layer that owns it.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from chassis.capabilities.keys import CapabilityRequirement
from chassis.core.collections import FrozenDict, freeze
from chassis.core.errors import ConfigurationError

__all__ = ["AgentRevision", "AgentSpec", "composition_payload"]

#: Default parent path under which an agent's scope is created when it declares none.
DEFAULT_AGENT_ROOT = "/agents"


def _validate_identifier(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ConfigurationError(
            f"agent {field_name} must be a non-empty, trimmed string", field=field_name
        )
    if "/" in value:
        raise ConfigurationError(
            f"agent {field_name} must not contain '/'", field=field_name, value=value
        )
    return value


def _validate_scope_path(path: str) -> str:
    if not path.startswith("/"):
        raise ConfigurationError("agent scope must be an absolute path", scope=path)
    if path != "/" and path.endswith("/"):
        raise ConfigurationError("agent scope must not end with '/'", scope=path)
    if "//" in path:
        raise ConfigurationError("agent scope must not contain empty segments", scope=path)
    for segment in path.split("/")[1:]:
        if segment in (".", ".."):
            raise ConfigurationError("agent scope must not contain '.' or '..'", scope=path)
    return path


def _names(values: Iterable[str], *, field_name: str) -> frozenset[str]:
    collected: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ConfigurationError(
                f"agent {field_name} entries must be non-empty, trimmed strings",
                field=field_name,
            )
        collected.add(value)
    return frozenset(collected)


def _requirements(values: Mapping[str, str], *, field_name: str) -> Mapping[str, str]:
    parsed: dict[str, str] = {}
    for name, specifier in values.items():
        _validate_identifier(name, field_name=f"{field_name} capability")
        # Parsed here so an invalid specifier is rejected when the spec is built,
        # not when it is first resolved. Chassis cannot know which capability
        # names exist, so only the specifier is validated.
        CapabilityRequirement.parse(name, specifier)
        parsed[name] = specifier
    return FrozenDict(parsed)


def _describe(value: Any) -> str:
    """Non-secret description of a programmatic plugin contribution."""

    if isinstance(value, type):
        return f"<{value.__name__}>"
    return f"<{type(value).__name__}>"


def _plugins(values: Mapping[str, Any]) -> Mapping[str, Any]:
    """Freeze declared plugin contributions.

    A value is either a configuration mapping (materialized after resolving the
    plugin name through the harness catalog) or a plugin type/instance supplied
    programmatically. Configuration and metadata never alias the author's
    containers.
    """

    frozen: dict[str, Any] = {}
    for name, value in values.items():
        _validate_identifier(name, field_name="plugin contribution")
        if value is None:
            frozen[name] = FrozenDict()
        elif isinstance(value, Mapping):
            frozen[name] = freeze(value)
        elif isinstance(value, type) or hasattr(value, "manifest"):
            frozen[name] = value
        else:
            raise ConfigurationError(
                "agent plugin contribution must be a configuration mapping, a plugin "
                "type, or a plugin instance",
                plugin=name,
                value_type=type(value).__name__,
            )
    return FrozenDict(frozen)


def _optional_text(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _validate_identifier(value, field_name=field_name)


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """An immutable description of the composition one logical agent sees.

    Args:
        name: Logical agent name. Stable across revisions. Not a memory address:
            it identifies the agent in diagnostics, snapshots, and attribution.
        revision: Author-declared revision of this composition. A revision, once
            published, is immutable; a composition-affecting change is a new
            revision. Two revisions never collapse merely because they materialize
            to a semantically equivalent composition.
        scope: Composition scope path the agent materializes into. Defaults to
            ``/agents/<name>``. Materialization is always through
            :class:`~chassis.composition.CompositionScope`; there is no second
            scope primitive.
        description: Human-readable description.
        runtime_ref: Logical reference to the execution runtime that runs this
            agent, resolved through the harness's registered runtimes. LangGraph
            types never appear here.
        capabilities: Capability view the agent scope exposes, or ``None`` for
            unrestricted. Narrowing only: a descendant can never widen it. This is
            composition visibility, not authorization.
        requires: Capability name to version specifier, declared as requirements
            *of the scope* and resolved against its visible providers.
        optional: Same, for requirements that must not gate anything.
        plugins: Plugin contributions materialized as entries local to the agent
            scope: implementation name to configuration mapping (resolved through
            the harness catalog), or a plugin type/instance for programmatic specs.
        tools: Tool names the agent scope exposes, or ``None`` for every tool
            visible from its lineage. Composition visibility, not authorization.
        profile: Engine-neutral label for the model/profile intent (for example
            ``"reasoning"``). Chassis never maps it to a vendor.
        metadata: Free-form, non-secret metadata attached to the agent scope.
    """

    name: str
    revision: str = "1"
    scope: str | None = None
    description: str = ""
    runtime_ref: str | None = None
    capabilities: Iterable[str] | None = None
    requires: Mapping[str, str] = field(default_factory=FrozenDict)
    optional: Mapping[str, str] = field(default_factory=FrozenDict)
    plugins: Mapping[str, Any] = field(default_factory=FrozenDict)
    tools: Iterable[str] | None = None
    profile: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_identifier(self.name, field_name="name"))
        object.__setattr__(
            self, "revision", _validate_identifier(self.revision, field_name="revision")
        )
        if self.scope is not None:
            object.__setattr__(self, "scope", _validate_scope_path(self.scope))
        object.__setattr__(
            self, "runtime_ref", _optional_text(self.runtime_ref, field_name="runtime_ref")
        )
        object.__setattr__(self, "profile", _optional_text(self.profile, field_name="profile"))
        if self.capabilities is not None:
            object.__setattr__(
                self, "capabilities", _names(self.capabilities, field_name="capabilities")
            )
        if self.tools is not None:
            object.__setattr__(self, "tools", _names(self.tools, field_name="tools"))
        object.__setattr__(self, "requires", _requirements(self.requires, field_name="requires"))
        object.__setattr__(self, "optional", _requirements(self.optional, field_name="optional"))
        object.__setattr__(self, "plugins", _plugins(self.plugins))
        if not isinstance(self.metadata, FrozenDict):
            object.__setattr__(self, "metadata", freeze(self.metadata))

    # ------------------------------------------------------------------ identity

    @property
    def identity(self) -> str:
        """Deterministic ``name@revision`` identity of this authored revision."""

        return f"{self.name}@{self.revision}"

    @property
    def scope_path(self) -> str:
        """Scope path this spec materializes into."""

        return self.scope if self.scope is not None else f"{DEFAULT_AGENT_ROOT}/{self.name}"

    # ------------------------------------------------------------------- output

    def to_dict(self) -> dict[str, Any]:
        """Structured, diagnostic-safe form.

        Plugin configuration and metadata values are reported by key only, exactly
        as the rest of Chassis reports configuration, so a spec can carry secret
        material without diagnostics becoming a place it accumulates.
        """

        return {
            "name": self.name,
            "revision": self.revision,
            "identity": self.identity,
            "scope": self.scope_path,
            "description": self.description,
            "runtime_ref": self.runtime_ref,
            "profile": self.profile,
            "capabilities": None if self.capabilities is None else sorted(self.capabilities),
            "requires": dict(sorted(self.requires.items())),
            "optional": dict(sorted(self.optional.items())),
            "tools": None if self.tools is None else sorted(self.tools),
            "plugins": {
                name: (sorted(value) if isinstance(value, Mapping) else _describe(value))
                for name, value in sorted(self.plugins.items())
            },
            "metadata_keys": sorted(str(key) for key in self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AgentSpec:
        """Build a spec from a declarative mapping.

        This is the supported way to construct a spec from configuration or
        generated definitions; it applies exactly the same validation as direct
        construction. ``plugins`` values are configuration mappings (or ``None``).
        Unknown keys are rejected rather than ignored.
        """

        allowed = {
            "name",
            "revision",
            "scope",
            "description",
            "runtime_ref",
            "capabilities",
            "requires",
            "optional",
            "plugins",
            "tools",
            "profile",
            "metadata",
        }
        unknown = sorted(set(payload) - allowed - {"identity", "metadata_keys"})
        if unknown:
            raise ConfigurationError("unknown agent spec field(s)", fields=unknown)
        if "name" not in payload:
            raise ConfigurationError("agent spec requires a name")
        plugins: dict[str, Any] = {}
        for name, value in dict(payload.get("plugins") or {}).items():
            if value is None:
                plugins[name] = None
            elif isinstance(value, Mapping):
                plugins[name] = dict(value)
            elif isinstance(value, (list, tuple)):
                # The diagnostic form of to_dict() reports configuration keys.
                # Decoding it would lose the values, so it decodes to no
                # configuration rather than pretending to reconstruct one.
                plugins[name] = None
            else:
                raise ConfigurationError(
                    "agent spec plugin configuration must be a mapping", plugin=name
                )
        return cls(
            name=payload["name"],
            revision=payload.get("revision", "1"),
            scope=payload.get("scope"),
            description=payload.get("description", ""),
            runtime_ref=payload.get("runtime_ref"),
            capabilities=payload.get("capabilities"),
            requires=dict(payload.get("requires") or {}),
            optional=dict(payload.get("optional") or {}),
            plugins=plugins,
            tools=payload.get("tools"),
            profile=payload.get("profile"),
            metadata=dict(payload.get("metadata") or {}),
        )


def composition_payload(spec: AgentSpec) -> dict[str, Any]:
    """Composition-affecting structure of a spec, before redaction.

    Used to compute a published revision's displayable composition digest: the
    caller hashes the *redacted* payload, so a secret-bearing configuration change
    still changes the digest without the digest being derived from the secret.
    """

    def contribution(value: Any) -> Any:
        if isinstance(value, Mapping):
            return dict(value)
        if value is None:
            return {}
        return _describe(value)

    return {
        "name": spec.name,
        "revision": spec.revision,
        "scope": spec.scope_path,
        "runtime_ref": spec.runtime_ref,
        "profile": spec.profile,
        "capabilities": None if spec.capabilities is None else sorted(spec.capabilities),
        "requires": dict(sorted(spec.requires.items())),
        "optional": dict(sorted(spec.optional.items())),
        "tools": None if spec.tools is None else sorted(spec.tools),
        "plugins": {name: contribution(value) for name, value in sorted(spec.plugins.items())},
    }


@dataclass(frozen=True, slots=True)
class AgentRevision:
    """A published, immutable revision of one agent's composition.

    A revision records the spec that was published, the scope it materialized
    into, the plugin entries it declared there, and a displayable composition
    digest. It is never mutated in place: publishing a different composition is a
    different revision. Old revisions stay reachable for attribution and
    diagnostics even after the agent is retired.
    """

    spec: AgentSpec
    scope: str
    entries: tuple[str, ...] = ()
    composition_digest: str = ""
    published_at: float = field(default_factory=time.time)

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def revision(self) -> str:
        return self.spec.revision

    @property
    def identity(self) -> str:
        return self.spec.identity

    @property
    def runtime_ref(self) -> str | None:
        return self.spec.runtime_ref

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.name,
            "revision": self.revision,
            "identity": self.identity,
            "scope": self.scope,
            "runtime_ref": self.runtime_ref,
            "entries": list(self.entries),
            "composition_digest": self.composition_digest,
            "published_at": self.published_at,
            "spec": self.spec.to_dict(),
        }
