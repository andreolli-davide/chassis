"""Agent definitions and the compiled-graph cache.

Build-time vs runtime-bound
---------------------------

Compiling a graph depends only on *build-time* inputs: the state schema, the node
and edge topology, the static tool composition, and middleware topology. A
runtime-bound change -- swapping the model provider, the database, the policy, or
the tenant -- must reuse the compiled graph and only change the capability
snapshot the run observes.

The cache key is therefore composed exclusively of declared build-time inputs::

    GraphCacheKey(
        agent,
        definition_version,
        state_schema_hash,
        tool_schema_hash,
        middleware_hash,
        build_time_versions,
    )

Nothing about the mutable runtime enters the key, so "any capability changed"
never invalidates every graph. The contract on agent authors is explicit: bump
``AgentDefinition.version`` when the node or edge topology changes, because that
is the one build-time input that cannot be derived from data.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph, StateGraph

from chassis.core.errors import ConfigurationError, GraphBuildError
from chassis.persistence.hashing import schema_hash, stable_hash, tool_schema_hash
from chassis.telemetry.base import NoopTelemetry, Telemetry
from chassis.tools.registry import ToolSnapshot

__all__ = [
    "AgentDefinition",
    "GraphBuildInputs",
    "GraphCache",
    "GraphCacheKey",
    "GraphCacheStats",
]


@dataclass(frozen=True, slots=True)
class GraphBuildInputs:
    """Everything a graph builder may use at construction time.

    Args:
        agent: Agent name.
        definition_version: Declared definition version.
        tools: Static tool set compiled into the graph.
        tool_snapshot: The snapshot those tools came from, for owner/policy access.
        build_time_versions: Versions of build-time capabilities this definition
            declares it depends on.
    """

    agent: str
    definition_version: str
    tools: tuple[BaseTool, ...]
    tool_snapshot: ToolSnapshot
    build_time_versions: Mapping[str, str] = field(default_factory=dict)

    def tool(self, name: str) -> BaseTool | None:
        for candidate in self.tools:
            if candidate.name == name:
                return candidate
        return None


GraphBuilder = Callable[[GraphBuildInputs], StateGraph[Any, Any, Any, Any]]


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """How to build one agent's graph, and what affects that build.

    Args:
        name: Agent name used for registration and invocation.
        version: Definition version. Bump it when the node or edge topology
            changes: it is the build-time input that cannot be derived from data.
        state_schema: Graph state schema. Its canonical hash is part of the cache
            key, so schema changes invalidate automatically.
        build: Builder invoked on a cache miss. It must be pure with respect to
            the inputs it receives.
        description: Human-readable description.
        build_time_capabilities: Capability names whose *versions* affect graph
            construction. Runtime-bound capabilities must not be listed here.
        middleware: Build-time middleware topology labels, hashed into the key.
    """

    name: str
    version: str
    state_schema: Any
    build: GraphBuilder
    description: str = ""
    build_time_capabilities: tuple[str, ...] = ()
    middleware: tuple[str, ...] = ()

    @property
    def identity(self) -> str:
        return f"{self.name}@{self.version}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "build_time_capabilities": list(self.build_time_capabilities),
            "middleware": list(self.middleware),
        }


@dataclass(frozen=True, slots=True)
class GraphCacheKey:
    """Identity of one compiled graph.

    Every field is a declared build-time input; none of them is a runtime-bound
    capability value.
    """

    agent: str
    definition_version: str
    state_schema_hash: str
    tool_schema_hash: str
    middleware_hash: str
    build_time_versions: tuple[tuple[str, str], ...] = ()

    def digest(self) -> str:
        return stable_hash(self.to_dict(), length=32)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "definition_version": self.definition_version,
            "state_schema_hash": self.state_schema_hash,
            "tool_schema_hash": self.tool_schema_hash,
            "middleware_hash": self.middleware_hash,
            "build_time_versions": dict(self.build_time_versions),
        }

    def __str__(self) -> str:
        return f"{self.agent}:{self.digest()}"


@dataclass(slots=True)
class GraphCacheStats:
    """Cache counters, for diagnostics and tracing."""

    hits: int = 0
    misses: int = 0
    builds: int = 0
    evictions: int = 0
    invalidations: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "builds": self.builds,
            "evictions": self.evictions,
            "invalidations": self.invalidations,
        }


class GraphCache:
    """Bounded LRU cache of compiled graphs, keyed by build-time inputs.

    Args:
        max_entries: Maximum number of compiled graphs retained. ``0`` disables
            caching entirely (every build is a miss); negative values are
            rejected.
        telemetry: Receives ``graph.compile`` and ``graph.cache`` signals.

    The key covers only *declared* build-time inputs (definition version, state
    schema, middleware topology, tool schema, build-time capability versions).
    Topology or captured static inputs the key cannot see require an
    :attr:`AgentDefinition.version` change; the cache cannot detect them.
    """

    def __init__(self, *, max_entries: int = 32, telemetry: Telemetry | None = None) -> None:
        if not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries < 0:
            raise ConfigurationError(
                "GraphCache max_entries must be a non-negative integer",
                max_entries=max_entries,
            )
        self._entries: OrderedDict[str, CompiledStateGraph[Any, Any, Any, Any]] = OrderedDict()
        self._keys: dict[str, GraphCacheKey] = {}
        self._max_entries = max_entries
        self._telemetry = telemetry if telemetry is not None else NoopTelemetry()
        self.stats = GraphCacheStats()

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def keys(self) -> tuple[GraphCacheKey, ...]:
        return tuple(self._keys.values())

    def get(self, key: GraphCacheKey) -> CompiledStateGraph[Any, Any, Any, Any] | None:
        """Return a cached graph, or ``None`` on a miss."""

        digest = key.digest()
        graph = self._entries.get(digest)
        if graph is None:
            self.stats.misses += 1
            self._telemetry.event("graph.cache", {**key.to_dict(), "result": "miss"})
            return None
        self._entries.move_to_end(digest)
        self.stats.hits += 1
        self._telemetry.event("graph.cache", {**key.to_dict(), "result": "hit"})
        return graph

    def put(self, key: GraphCacheKey, graph: CompiledStateGraph[Any, Any, Any, Any]) -> None:
        """Store a compiled graph under ``key``."""

        digest = key.digest()
        self._entries[digest] = graph
        self._keys[digest] = key
        self._entries.move_to_end(digest)
        self.stats.builds += 1
        while len(self._entries) > self._max_entries:
            evicted, _ = self._entries.popitem(last=False)
            self._keys.pop(evicted, None)
            self.stats.evictions += 1

    def invalidate(self, agent: str | None = None) -> int:
        """Drop cached graphs, optionally only for one agent. Returns how many."""

        digests = [
            digest for digest, key in self._keys.items() if agent is None or key.agent == agent
        ]
        for digest in digests:
            self._entries.pop(digest, None)
            self._keys.pop(digest, None)
        self.stats.invalidations += len(digests)
        if digests:
            self._telemetry.event(
                "graph.cache.invalidate", {"agent": agent or "*", "entries": len(digests)}
            )
        return len(digests)

    def clear(self) -> None:
        self.invalidate(None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "size": len(self._entries),
            "max_entries": self._max_entries,
            "stats": self.stats.to_dict(),
            "keys": [key.to_dict() for key in self._keys.values()],
        }


def build_cache_key(
    definition: AgentDefinition,
    *,
    tools: Sequence[BaseTool],
    build_time_versions: Mapping[str, str] | None = None,
) -> GraphCacheKey:
    """Compose the cache key for one definition and one static tool set."""

    try:
        state_hash = schema_hash(definition.state_schema)
    except Exception as error:
        raise GraphBuildError(
            f"state schema of agent {definition.name!r} cannot be hashed",
            agent=definition.name,
        ) from error
    return GraphCacheKey(
        agent=definition.name,
        definition_version=definition.version,
        state_schema_hash=state_hash,
        tool_schema_hash=tool_schema_hash(tools),
        middleware_hash=stable_hash(list(definition.middleware), length=32),
        build_time_versions=tuple(sorted((build_time_versions or {}).items())),
    )
