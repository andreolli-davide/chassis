"""Immutable runtime snapshots.

Every run should be attributable to the composition that produced it. A snapshot
is descriptive metadata -- not a live object graph -- built from canonical data so
its hash is stable, and redacted before anything is hashed or emitted.

Snapshots never contain secret material: configuration is redacted first, and the
remaining hashes cover structure (plugin identities, dependency edges, tool
contracts, schemas), not payloads.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from chassis.core.collections import frozen_mapping
from chassis.core.errors import FormatError
from chassis.persistence.formats import SNAPSHOT_FORMAT_VERSION, migrate_payload
from chassis.persistence.hashing import stable_hash, tool_schema_hash
from chassis.secrets.redaction import SecretRedactor, redact_config
from chassis.tools.registry import ToolSnapshot

if TYPE_CHECKING:
    # Typing only: the generation type lives above the plugin layer, and importing
    # it here would make this module part of that layer's import cycle.
    from chassis.core.generation import RuntimeGeneration

__all__ = ["RuntimeSnapshot", "chassis_version"]


def chassis_version() -> str:
    """Installed Chassis version, or ``"0.0.0"`` when running from a source tree."""

    try:
        # The distribution is ``chassis-harness``; the import package stays ``chassis``.
        return version("chassis-harness")
    except PackageNotFoundError:  # pragma: no cover - only outside an install
        return "0.0.0"


def _jsonable(value: Any) -> Any:
    """Render deeply frozen containers back as JSON-compatible dicts and lists.

    Storage is frozen against mutation; the canonical rendering stays the
    JSON shape callers and hashes expect.
    """

    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """Descriptive metadata identifying the runtime composition of a run."""

    chassis_version: str
    generation_id: str
    sequence: int
    agent_runtime: str
    plugins: Mapping[str, str]
    capabilities: Mapping[str, tuple[str, ...]]
    config_hash: str
    plugin_graph_hash: str
    tool_schema_hash: str
    graph_definition_hash: str | None = None
    prompt_hash: str | None = None
    agent: str | None = None
    agent_revision: str | None = None
    created_at: float = field(default_factory=time.time)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    scopes: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    runtime_instance_ids: tuple[str, ...] = ()
    semantic_scopes: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "plugins", frozen_mapping(self.plugins))
        object.__setattr__(self, "capabilities", frozen_mapping(self.capabilities))
        object.__setattr__(self, "runtime_instance_ids", tuple(self.runtime_instance_ids))
        if not isinstance(self.semantic_scopes, MappingProxyType):
            object.__setattr__(self, "semantic_scopes", frozen_mapping(self.semantic_scopes))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", frozen_mapping(self.metadata))
        if not isinstance(self.scopes, MappingProxyType):
            object.__setattr__(self, "scopes", frozen_mapping(self.scopes))

    def to_dict(self) -> dict[str, Any]:
        """Canonical, JSON-compatible representation used for hashing and emission.

        The record declares its serialization format version (never the package
        version) and is self-contained: :meth:`from_dict` rebuilds an identical
        record, semantic scope tree included.
        """

        return _jsonable(
            {
                "format_version": SNAPSHOT_FORMAT_VERSION,
                "chassis_version": self.chassis_version,
                "generation_id": self.generation_id,
                "sequence": self.sequence,
                "agent_runtime": self.agent_runtime,
                "agent": self.agent,
                "agent_revision": self.agent_revision,
                "plugins": dict(sorted(self.plugins.items())),
                "capabilities": {
                    name: list(versions) for name, versions in sorted(self.capabilities.items())
                },
                "config_hash": self.config_hash,
                "plugin_graph_hash": self.plugin_graph_hash,
                "graph_definition_hash": self.graph_definition_hash,
                "tool_schema_hash": self.tool_schema_hash,
                "prompt_hash": self.prompt_hash,
                "metadata": dict(sorted(self.metadata.items(), key=lambda item: str(item[0]))),
                "scopes": self.scopes,
                "semantic_scopes": self.semantic_scopes,
                "runtime_instance_ids": list(self.runtime_instance_ids),
            }
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RuntimeSnapshot:
        """Rebuild a snapshot record from its serialized form.

        Dispatches explicitly on the declared format version. A pre-versioning
        0.8.1-era record (which declared none) is migrated and preserves
        everything 0.8.1 persisted — generation identity and sequence, agent
        identity and revision, capability versions, hashes, metadata, scope
        topology, and runtime instance ids. Its semantic scope tree, which 0.8.1
        did not persist, is reconstructed from the recorded topology with empty
        provider maps: 0.8.1 recorded provider *instance* ids, which cannot be
        mapped back to entries, so ``semantic_digest()`` of a migrated record
        covers the reconstructed view only.

        The record carries no creation time (neither did 0.8.1): a rebuilt
        snapshot reports ``created_at`` 0.0 unless the payload declares one.

        Raises:
            FormatError: ``future_version``, ``malformed_version``,
                ``unmigratable``, or ``corrupted`` (a payload that does not
                match its declared shape) — never a silent reinterpretation.
        """

        document = migrate_payload(
            payload,
            format_name="snapshot",
            supported=SNAPSHOT_FORMAT_VERSION,
            migrations=_SNAPSHOT_MIGRATIONS,
        )

        def corrupt(detail: str) -> FormatError:
            return FormatError(
                f"snapshot document is corrupted: {detail}",
                format="snapshot",
                reason="corrupted",
            )

        def value(key: str, expected: tuple[type, ...], *, optional: bool = False) -> Any:
            if key not in document:
                if optional:
                    return None
                raise corrupt(f"missing {key!r}")
            found = document[key]
            if found is None and optional:
                return None
            if isinstance(found, bool) and bool not in expected:
                raise corrupt(f"{key!r} has the wrong type")
            if not isinstance(found, expected):
                raise corrupt(f"{key!r} has the wrong type")
            return found

        def strings(key: str) -> tuple[str, ...]:
            items = value(key, (list, tuple))
            if not all(isinstance(item, str) for item in items):
                raise corrupt(f"{key!r} must contain strings")
            return tuple(items)

        plugins = value("plugins", (Mapping,))
        capabilities = value("capabilities", (Mapping,))
        metadata = value("metadata", (Mapping,), optional=True) or {}
        if not all(
            isinstance(name, str) and isinstance(item, str) for name, item in plugins.items()
        ):
            raise corrupt("'plugins' must map names to versions")
        if not all(
            isinstance(name, str)
            and isinstance(items, (list, tuple))
            and all(isinstance(item, str) for item in items)
            for name, items in capabilities.items()
        ):
            raise corrupt("'capabilities' must map names to version lists")

        created_at = value("created_at", (int, float), optional=True)
        sequence = value("sequence", (int,))

        return cls(
            chassis_version=value("chassis_version", (str,)),
            generation_id=value("generation_id", (str,)),
            sequence=sequence,
            agent_runtime=value("agent_runtime", (str,)),
            plugins=dict(plugins),
            capabilities={name: tuple(items) for name, items in capabilities.items()},
            config_hash=value("config_hash", (str,)),
            plugin_graph_hash=value("plugin_graph_hash", (str,)),
            tool_schema_hash=value("tool_schema_hash", (str,)),
            graph_definition_hash=value("graph_definition_hash", (str,), optional=True),
            prompt_hash=value("prompt_hash", (str,), optional=True),
            agent=value("agent", (str,), optional=True),
            agent_revision=value("agent_revision", (str,), optional=True),
            created_at=float(created_at) if created_at is not None else 0.0,
            metadata=dict(metadata),
            scopes=dict(value("scopes", (Mapping,))),
            runtime_instance_ids=strings("runtime_instance_ids"),
            semantic_scopes=dict(value("semantic_scopes", (Mapping,)) or {}),
        )

    def semantic_composition(self) -> dict[str, Any]:
        """The generation's semantic composition, independent of how it was built.

        Includes what a consumer can observe about composition: plugins,
        capabilities, dependency edges, tool contracts, redacted configuration
        hash, and the resolved scope tree. It deliberately excludes the generation
        id, sequence, creation time, metadata, and runtime instance ids, so two
        semantically equivalent generations that were materialised separately
        share a semantic digest.
        """

        return {
            "plugins": dict(sorted(self.plugins.items())),
            "capabilities": {
                name: list(versions) for name, versions in sorted(self.capabilities.items())
            },
            "config_hash": self.config_hash,
            "plugin_graph_hash": self.plugin_graph_hash,
            "tool_schema_hash": self.tool_schema_hash,
            "scopes": self.semantic_scopes,
        }

    def semantic_digest(self) -> str:
        """Stable hash of the semantic composition only (see :meth:`semantic_composition`)."""

        return stable_hash(self.semantic_composition())

    def public_composition_digest(self) -> str:
        """Additive alias for :meth:`semantic_digest`.

        The name states what the digest is: the *public* composition identity a
        consumer can observe, as opposed to :meth:`physical_digest`, which covers
        the runtime instances. ``semantic_digest`` is unchanged and remains the
        supported name.
        """

        return self.semantic_digest()

    @property
    def agent_identity(self) -> str | None:
        """``name@revision`` identity of the agent this snapshot describes."""

        if self.agent is None:
            return None
        if self.agent_revision is None:
            return self.agent
        return f"{self.agent}@{self.agent_revision}"

    def physical_digest(self) -> str:
        """Stable hash of the runtime instances this snapshot was published with.

        Separate from :meth:`semantic_digest` on purpose: it changes when a
        composition is rebuilt even if it is semantically identical, which is the
        distinction between "semantically unchanged" and "physically reused".
        """

        return stable_hash(list(self.runtime_instance_ids))

    def digest(self) -> str:
        """Stable hash of the whole snapshot record."""

        return stable_hash(self.to_dict())

    @classmethod
    def from_generation(
        cls,
        generation: RuntimeGeneration,
        *,
        tools: ToolSnapshot | None = None,
        redactor: SecretRedactor | None = None,
        agent: str | None = None,
        agent_revision: str | None = None,
        agent_runtime: str = "unknown",
        graph_definition_hash: str | None = None,
        prompt_hash: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RuntimeSnapshot:
        """Build a snapshot for one published generation."""

        effective_redactor = redactor if redactor is not None else SecretRedactor()
        return cls(
            chassis_version=chassis_version(),
            generation_id=generation.generation_id,
            sequence=generation.sequence,
            agent_runtime=agent_runtime,
            agent=agent,
            agent_revision=agent_revision,
            plugins={
                instance.manifest.name: instance.manifest.version
                for instance in generation.instances
            },
            capabilities=_capability_versions(generation),
            config_hash=_config_hash(generation, effective_redactor),
            plugin_graph_hash=_plugin_graph_hash(generation),
            tool_schema_hash=_tools_hash(tools),
            graph_definition_hash=graph_definition_hash,
            prompt_hash=prompt_hash,
            created_at=generation.created_at,
            metadata=effective_redactor.redact_value(dict(metadata or {})),
            scopes=generation.scopes.fingerprint(),
            runtime_instance_ids=generation.instance_ids,
            semantic_scopes=_semantic_scopes(generation),
        )


def _semantic_scopes(generation: RuntimeGeneration) -> dict[str, Any]:
    """Scope topology with instance ids replaced by semantic entry ids.

    The published :meth:`ScopeTree.fingerprint` names the runtime instances that
    provide a capability, which is exactly right for explaining *this* generation
    but wrong for comparing two semantically equivalent ones. This view keeps the
    semantic structure -- paths, capability views, entries, requirement selections
    by provider entry -- and drops physical identity.
    """

    entry_by_instance = {
        instance.instance_id: instance.entry_id for instance in generation.instances
    }

    def entry_ids(ids: tuple[str, ...]) -> list[str]:
        return sorted(entry_by_instance.get(instance_id, instance_id) for instance_id in ids)

    return {
        "root": generation.scopes.root,
        "scopes": [
            {
                "path": scope.path,
                "parent": scope.parent,
                "children": list(scope.children),
                "capabilities": (None if scope.capabilities is None else list(scope.capabilities)),
                "tools": (None if scope.tools is None else list(scope.tools)),
                "entries": list(scope.entries),
                "providers": {
                    name: entry_ids(ids) for name, ids in sorted(scope.providers.items())
                },
                "visible_tools": list(scope.visible_tools),
                "selections": [
                    {
                        "consumer": item.consumer,
                        "requirement": str(item.requirement),
                        "status": item.status,
                        "provider": item.provider_entry_id,
                        "provider_scope": item.provider_scope,
                    }
                    for item in scope.provenance
                ],
            }
            for scope in generation.scopes
        ],
    }


def _capability_versions(generation: RuntimeGeneration) -> dict[str, tuple[str, ...]]:
    collected: dict[str, list[str]] = {}
    for registration in generation.snapshot.registrations:
        collected.setdefault(registration.key.name, []).append(str(registration.version))
    return {name: tuple(sorted(versions)) for name, versions in sorted(collected.items())}


def _config_hash(generation: RuntimeGeneration, redactor: SecretRedactor) -> str:
    payload = {
        instance.entry_id: redact_config(dict(instance.config), redactor)
        for instance in generation.instances
    }
    return stable_hash(payload)


def _plugin_graph_hash(generation: RuntimeGeneration) -> str:
    """Hash of the plugin dependency graph: who provides what to whom."""

    by_instance = {instance.instance_id: instance.entry_id for instance in generation.instances}
    nodes = sorted(
        (instance.entry_id, instance.manifest.identity) for instance in generation.instances
    )
    edges = sorted(
        {
            (by_instance[registration.provider_id], instance.entry_id)
            for instance in generation.instances
            for registration in instance.resolved.values()
            if registration.provider_id in by_instance
        }
    )
    return stable_hash({"nodes": nodes, "edges": edges})


def _tools_hash(tools: ToolSnapshot | None) -> str:
    if tools is None:
        return stable_hash([])
    return tool_schema_hash([entry.tool for entry in tools.entries])


def _reconstructed_semantic_scopes(scopes: Mapping[str, Any]) -> dict[str, Any]:
    """Semantic scope view recoverable from a recorded (physical) scope tree.

    Everything except provider identity is recorded one-for-one. Provider maps
    stay empty: the recorded tree names provider *instance* ids, and an instance
    to entry mapping is not part of the record, so inferring it would be a
    guess — the documented 0.8.1 migration limitation.
    """

    recorded = scopes.get("scopes")
    entries: list[dict[str, Any]] = []
    for scope in recorded if isinstance(recorded, list) else []:
        if not isinstance(scope, Mapping):
            continue
        entries.append(
            {
                "path": scope.get("path"),
                "parent": scope.get("parent"),
                "children": list(scope.get("children") or []),
                "capabilities": scope.get("capabilities"),
                "tools": scope.get("tools"),
                "entries": list(scope.get("entries") or []),
                "providers": {},
                "visible_tools": list(scope.get("visible_tools") or []),
                "selections": list(scope.get("selections") or []),
            }
        )
    return {"root": scopes.get("root"), "scopes": entries}


def _migrate_snapshot_v0(document: dict[str, Any]) -> dict[str, Any]:
    """Migrate a pre-versioning (0.8.1-era) snapshot record to format 1."""

    migrated = dict(document)
    recorded_scopes = document.get("scopes")
    scopes: Mapping[str, Any] = recorded_scopes if isinstance(recorded_scopes, Mapping) else {}
    migrated["semantic_scopes"] = _reconstructed_semantic_scopes(scopes)
    return migrated


_SNAPSHOT_MIGRATIONS = {0: _migrate_snapshot_v0}
