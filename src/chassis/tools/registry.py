"""Scope-owned registry of tools.

Registration wraps a tool object with harness metadata; it never replaces the
tool. Registration is owned by the scope that created it, so a tool cannot
outlive its plugin and unregistering is not the plugin author's job.

A registrable tool is anything satisfying the structural :class:`Tool` contract:
a ``name``, a ``description``, and an awaitable ``ainvoke``. Chassis core does not
depend on a tool library; a ``langchain-core`` ``BaseTool`` satisfies the contract
by construction, which is what lets the LangGraph adapter compile registered tools
into a graph without the kernel importing LangGraph.

A tool the harness executes must come from the generation the run acquired, so
execution takes an immutable :class:`ToolSnapshot` rather than reading the live
registry.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any, Protocol

from chassis.core.errors import ConfigurationError
from chassis.core.scope import Scope
from chassis.tools.metadata import ToolPolicy

__all__ = ["RegisteredTool", "ScopedTools", "Tool", "ToolRegistry", "ToolSnapshot"]


class Tool(Protocol):
    """Structural contract a registrable tool must satisfy.

    Deliberately narrow: Chassis owns registration, ownership, and execution
    boundaries, not the tool abstraction. ``langchain-core``'s ``BaseTool``
    satisfies this protocol, as does any object with the same shape. Validation at
    registration is duck-typed (see :func:`_is_tool`), so this protocol documents
    the contract rather than gating it at runtime.
    """

    @property
    def name(self) -> str:
        """Stable tool name used in snapshots, policy, and tool requests."""

        ...

    @property
    def description(self) -> str:
        """Human-readable description, surfaced in diagnostics."""

        ...

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        """Execute the tool asynchronously."""

        ...


def _is_tool(tool: object) -> bool:
    """Whether ``tool`` satisfies the structural contract, without importing one."""

    return callable(getattr(tool, "ainvoke", None)) and isinstance(getattr(tool, "name", None), str)


class ToolNotFound(ConfigurationError):
    """Raised when a tool is not present in the snapshot a run is using."""

    code = "tool_not_found"


class RegisteredTool:
    """A tool plus the harness semantics attached to it."""

    __slots__ = (
        "metadata",
        "owner_id",
        "owner_name",
        "policy",
        "registration_id",
        "scope_id",
        "tool",
    )

    def __init__(
        self,
        *,
        registration_id: str,
        tool: Tool,
        policy: ToolPolicy,
        owner_id: str,
        owner_name: str,
        scope_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.registration_id = registration_id
        self.tool = tool
        self.policy = policy
        self.owner_id = owner_id
        self.owner_name = owner_name
        self.scope_id = scope_id
        self.metadata: Mapping[str, Any] = MappingProxyType(dict(metadata or {}))

    @property
    def name(self) -> str:
        return self.tool.name

    @property
    def description(self) -> str:
        return self.tool.description

    def __repr__(self) -> str:
        return (
            f"RegisteredTool(name={self.name!r}, owner={self.owner_name!r}, "
            f"policy={self.policy.to_dict()!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "registration_id": self.registration_id,
            "name": self.name,
            "description": self.description,
            "owner_id": self.owner_id,
            "owner_name": self.owner_name,
            "scope_id": self.scope_id,
            "policy": self.policy.to_dict(),
            "metadata": dict(self.metadata),
        }


class ToolSnapshot:
    """Immutable view of the tools reachable from one runtime generation."""

    __slots__ = ("_entries", "_generation_id")

    def __init__(self, generation_id: str, entries: Iterable[RegisteredTool]) -> None:
        self._generation_id = generation_id
        self._entries = tuple(sorted(entries, key=lambda entry: entry.name))

    @property
    def generation_id(self) -> str:
        return self._generation_id

    @property
    def entries(self) -> tuple[RegisteredTool, ...]:
        return self._entries

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self._entries)

    def get(self, name: str) -> RegisteredTool | None:
        for entry in self._entries:
            if entry.name == name:
                return entry
        return None

    def require(self, name: str) -> RegisteredTool:
        """Return the registered tool named ``name``.

        Raises:
            ToolNotFound: the tool is not part of this snapshot.
        """

        entry = self.get(name)
        if entry is None:
            raise ToolNotFound(
                f"tool {name!r} is not available in generation {self._generation_id}",
                tool=name,
                generation_id=self._generation_id,
                available=list(self.names),
            )
        return entry

    def to_tools(self) -> list[Tool]:
        """The underlying tools, for graph or ``ToolNode`` composition."""

        return [entry.tool for entry in self._entries]

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get(name) is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation_id": self._generation_id,
            "tools": [entry.to_dict() for entry in self._entries],
        }


class ToolRegistry:
    """Live registry of tools, with scope-owned registrations."""

    def __init__(self) -> None:
        self._entries: dict[str, RegisteredTool] = {}

    # ------------------------------------------------------------- registration

    def register(
        self,
        *,
        scope: Scope,
        tool: Tool,
        policy: ToolPolicy | None = None,
        owner_id: str = "",
        owner_name: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> RegisteredTool:
        """Register ``tool`` for the lifetime of ``scope``.

        Raises:
            ConfigurationError: the object does not satisfy the tool contract
                (no non-empty ``name`` or no awaitable ``ainvoke``), or a tool
                with that name is already registered.
        """

        scope.assert_open(f"register tool {getattr(tool, 'name', '?')}")
        if not _is_tool(tool) or not tool.name:
            raise ConfigurationError(
                "registered tools must expose a non-empty name and an awaitable ainvoke",
                tool=type(tool).__name__,
            )
        name = tool.name
        if name in self._entries:
            raise ConfigurationError(
                "a tool with this name is already registered",
                tool=name,
                owner=self._entries[name].owner_name,
            )
        entry = RegisteredTool(
            registration_id=f"tool_{uuid.uuid4().hex[:12]}",
            tool=tool,
            policy=policy or ToolPolicy(),
            owner_id=owner_id,
            owner_name=owner_name or owner_id,
            scope_id=scope.id,
            metadata=metadata,
        )
        self._entries[name] = entry
        scope.cleanup(f"tool {name}", self.unregister, name, kind="tool")
        return entry

    def unregister(self, name: str) -> bool:
        """Remove a tool by name. Returns whether it was present."""

        return self._entries.pop(name, None) is not None

    # ------------------------------------------------------------------ reading

    def get(self, name: str) -> RegisteredTool | None:
        return self._entries.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    def entries(self) -> tuple[RegisteredTool, ...]:
        return tuple(self._entries[name] for name in self.names())

    def __len__(self) -> int:
        return len(self._entries)

    def snapshot(
        self, generation_id: str, *, owner_ids: Iterable[str] | None = None
    ) -> ToolSnapshot:
        """Immutable view, optionally restricted to tools owned by given instances.

        ``owner_ids=None`` means every registration; an empty iterable means none.
        """

        if owner_ids is None:
            selected = self.entries()
        else:
            owners = set(owner_ids)
            selected = tuple(entry for entry in self.entries() if entry.owner_id in owners)
        return ToolSnapshot(generation_id, selected)

    def to_dict(self) -> dict[str, Any]:
        return {"tools": [entry.to_dict() for entry in self.entries()]}


class ScopedTools:
    """Plugin-facing facade bound to one plugin instance scope."""

    __slots__ = ("_owner_id", "_owner_name", "_registry", "_scope")

    def __init__(
        self,
        registry: ToolRegistry,
        scope: Scope,
        owner_id: str,
        owner_name: str,
    ) -> None:
        self._registry = registry
        self._scope = scope
        self._owner_id = owner_id
        self._owner_name = owner_name

    def register(
        self,
        tool: Tool,
        *,
        policy: ToolPolicy | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RegisteredTool:
        """Register a tool for the lifetime of the plugin scope."""

        return self._registry.register(
            scope=self._scope,
            tool=tool,
            policy=policy,
            owner_id=self._owner_id,
            owner_name=self._owner_name,
            metadata=metadata,
        )

    def unregister(self, name: str) -> bool:
        """Remove a tool early, before the scope closes."""

        return self._registry.unregister(name)

    @property
    def names(self) -> tuple[str, ...]:
        """Names of the tools this plugin currently owns."""

        return tuple(
            entry.name for entry in self._registry.entries() if entry.owner_id == self._owner_id
        )
