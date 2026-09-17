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
    created_at: float = field(default_factory=time.time)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    scopes: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    runtime_instance_ids: tuple[str, ...] = ()
    semantic_scopes: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "plugins", MappingProxyType(dict(self.plugins)))
        object.__setattr__(self, "capabilities", MappingProxyType(dict(self.capabilities)))
        object.__setattr__(self, "runtime_instance_ids", tuple(self.runtime_instance_ids))
        if not isinstance(self.semantic_scopes, MappingProxyType):
            object.__setattr__(
                self, "semantic_scopes", MappingProxyType(dict(self.semantic_scopes))
            )
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        if not isinstance(self.scopes, MappingProxyType):
            object.__setattr__(self, "scopes", MappingProxyType(dict(self.scopes)))

    def to_dict(self) -> dict[str, Any]:
        """Canonical, JSON-compatible representation used for hashing and emission."""

        return {
            "chassis_version": self.chassis_version,
            "generation_id": self.generation_id,
            "sequence": self.sequence,
            "agent_runtime": self.agent_runtime,
            "agent": self.agent,
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
            "runtime_instance_ids": list(self.runtime_instance_ids),
        }

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
        agent_runtime: str = "langgraph",
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
