"""Hierarchical composition scopes.

A *composition scope* is a named node in a candidate composition. It is not an
agent, a tenant, or a session: those are use cases that a future release models
*with* this primitive. A scope may:

- declare local plugin entries (it owns them);
- inherit the providers visible from its ancestors;
- declare its own capability requirements;
- narrow the composition it exposes through a capability view;
- nest, and contribute to dependency resolution and diagnostics.

Derived composition view, not a mutable overlay
-----------------------------------------------

Composition scopes exist in the control plane as desired state
(:class:`CompositionScope`) and become part of a published generation as an
immutable, resolved view (:class:`ScopeTree` of :class:`ResolvedScope`)::

    desired state -> candidate generation -> root scope -> child scopes
                  -> resolve -> validate -> publish immutable generation

There is no path from a published generation back to a mutable scope: any
scope-affecting change is a desired-state change and becomes visible only through
the publication of a new generation (guarantee G16).

Visibility
----------

A consumer in scope ``S`` may observe the local providers of ``S`` and of its
ancestors, filtered by the capability view of every scope on the path from the
root to ``S`` (guarantees G15). A parent never implicitly observes a child's local
providers, and siblings never observe each other's.

Ownership
---------

A composition scope owns the entries it declares. Physical ownership stays with
the existing :class:`~chassis.core.scope.Scope` of each mounted plugin instance,
so scoped composition introduces no second teardown system: rolling back a
candidate, withdrawing a registration, or disposing an unreachable instance all
keep working exactly as they do for a flat composition.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from chassis.capabilities.keys import CapabilityKey, CapabilityRequirement
from chassis.capabilities.registry import CapabilityRegistration
from chassis.core.collections import freeze, frozen_mapping
from chassis.core.errors import ConfigurationError
from chassis.core.identity import (
    DependencyBinding,
    ImpactAnalysis,
    NodeImpact,
    ReuseDecision,
    ReuseReason,
    SemanticIdentity,
)
from chassis.core.paths import canonical_scope_path
from chassis.plugins.lifecycle import PluginInstance
from chassis.plugins.resolver import (
    ROOT_SCOPE,
    RequirementResolution,
    ResolutionPlan,
    ScopePlan,
)
from chassis.tools.registry import RegisteredTool

__all__ = [
    "ROOT_NAME",
    "ROOT_PATH",
    "CompositionScope",
    "CompositionTree",
    "DependencyBinding",
    "ImpactAnalysis",
    "NodeImpact",
    "ResolvedScope",
    "ReuseDecision",
    "ReuseReason",
    "ScopeSpec",
    "ScopeTree",
    "SemanticIdentity",
    "build_scope_tree",
]

#: Path of the implicit root scope of every composition.
ROOT_PATH = ROOT_SCOPE
#: Name of the implicit root scope.
ROOT_NAME = "root"

_PATH_SEPARATOR = "/"


def _join_path(parent: str, name: str) -> str:
    if parent == ROOT_PATH:
        return f"{ROOT_PATH}{name}"
    return f"{parent}{_PATH_SEPARATOR}{name}"


def _validate_name(name: str) -> str:
    if not name or name != name.strip():
        raise ConfigurationError("composition scope name must not be empty", name=name)
    if _PATH_SEPARATOR in name:
        raise ConfigurationError("composition scope name must not contain '/'", name=name)
    if name in (".", ".."):
        raise ConfigurationError("composition scope name is reserved", name=name)
    return name


def _validate_parent_ownership(
    tree: CompositionTree, parent: CompositionScope | None, name: str
) -> None:
    if parent is not None and parent.tree is not tree:
        raise ConfigurationError(
            "parent scope belongs to another composition tree",
            name=name,
            parent=parent.path,
        )


def _view_from(capabilities: Iterable[str | CapabilityKey] | None) -> frozenset[str] | None:
    if capabilities is None:
        return None
    return frozenset(
        item.name if isinstance(item, CapabilityKey) else item for item in capabilities
    )


def _tool_view_from(tools: Iterable[str] | None) -> frozenset[str] | None:
    if tools is None:
        return None
    return frozenset(tools)


@dataclass(frozen=True, slots=True)
class ScopeSpec:
    """Structural description of one composition scope.

    This is what the resolver consumes: it carries no control-plane behaviour and
    no references to mutable objects, so a resolution is a pure function of it.
    """

    path: str
    name: str
    parent: str | None
    capabilities: frozenset[str] | None = None
    tools: frozenset[str] | None = None
    requirements: tuple[CapabilityRequirement, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", frozen_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        """Structured form. Metadata is never included."""

        return {
            "path": self.path,
            "name": self.name,
            "parent": self.parent,
            "capabilities": None if self.capabilities is None else sorted(self.capabilities),
            "tools": None if self.tools is None else sorted(self.tools),
            "requirements": [str(item) for item in self.requirements],
        }


@runtime_checkable
class _ScopeOwner(Protocol):
    """The control plane operations a composition tree performs."""

    def install(
        self,
        plugin: Any,
        *,
        entry_id: str | None = None,
        config: Mapping[str, object] | None = None,
        replace: bool = False,
        scope: CompositionScope | str | None = None,
    ) -> str: ...

    def uninstall(self, entry_id: str) -> bool: ...

    def entries_for_scope(self, path: str) -> tuple[str, ...]: ...

    def mark_dirty(self) -> None: ...


class CompositionScope:
    """A node of the desired-state composition tree.

    Mutable by design: it is control-plane desired state, not published
    composition. Instances are created through :class:`CompositionTree`, so a
    scope always has a tree and a path.
    """

    __slots__ = (
        "_capabilities",
        "_children",
        "_metadata",
        "_name",
        "_parent",
        "_requirements",
        "_tools",
        "_tree",
    )

    def __init__(
        self,
        tree: CompositionTree,
        name: str,
        *,
        parent: CompositionScope | None = None,
        capabilities: Iterable[str | CapabilityKey] | None = None,
        tools: Iterable[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._tree = tree
        self._name = _validate_name(name)
        _validate_parent_ownership(tree, parent, name)
        self._parent = parent
        self._capabilities = _view_from(capabilities)
        self._tools = _tool_view_from(tools)
        self._metadata: Mapping[str, Any] = freeze(metadata or {})
        self._children: list[CompositionScope] = []
        self._requirements: dict[str, CapabilityRequirement] = {}

    # ------------------------------------------------------------------ identity

    @property
    def name(self) -> str:
        return self._name

    @property
    def parent(self) -> CompositionScope | None:
        return self._parent

    @property
    def path(self) -> str:
        """Deterministic identity of this scope within the composition.

        Root is ``"/"``; a child of root is ``"/research"``; a grandchild is
        ``"/tenant:acme/research"``. The path is derived from names, never from
        object identity or memory addresses.
        """

        if self._parent is None:
            return ROOT_PATH
        return _join_path(self._parent.path, self._name)

    @property
    def is_root(self) -> bool:
        return self._parent is None

    @property
    def children(self) -> tuple[CompositionScope, ...]:
        return tuple(self._children)

    def add_child(self, scope: CompositionScope) -> None:
        """Register a child scope. Called by :class:`CompositionTree`."""

        self._children.append(scope)

    def remove_child(self, scope: CompositionScope) -> None:
        """Forget a child scope. Called by :class:`CompositionTree`."""

        self._children.remove(scope)

    @property
    def tree(self) -> CompositionTree:
        return self._tree

    @property
    def metadata(self) -> Mapping[str, Any]:
        """Non-secret scope metadata, read-only."""

        return MappingProxyType(self._metadata)

    def set_metadata(self, metadata: Mapping[str, Any]) -> None:
        """Replace this scope's metadata. Never store secret values here."""

        self._metadata = freeze(metadata)
        self._tree.invalidate()

    @property
    def capabilities(self) -> frozenset[str] | None:
        """The capability view this scope exposes, or ``None`` for unrestricted."""

        return self._capabilities

    @property
    def tools(self) -> frozenset[str] | None:
        """The tool view this scope exposes, or ``None`` for every visible tool.

        Tool visibility is composition visibility, not authorization: a tool this
        scope exposes is not thereby authorized for a user or an action.
        """

        return self._tools

    # --------------------------------------------------------------- structure

    def child(
        self,
        name: str,
        *,
        capabilities: Iterable[str | CapabilityKey] | None = None,
        tools: Iterable[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> CompositionScope:
        """Create a nested composition scope."""

        return self._tree.attach(
            CompositionScope(
                self._tree,
                name,
                parent=self,
                capabilities=capabilities,
                tools=tools,
                metadata=metadata,
            )
        )

    # ----------------------------------------------------------- registration

    def install(
        self,
        plugin: Any,
        *,
        entry_id: str | None = None,
        config: Mapping[str, object] | None = None,
        replace: bool = False,
    ) -> str:
        """Declare a plugin entry local to this scope, owned by it.

        The entry participates in dependency resolution with this scope as its
        visibility context. Desired-state changes take effect on the next
        reconciliation.
        """

        return self._tree.owner.install(
            plugin, entry_id=entry_id, config=config, replace=replace, scope=self
        )

    def uninstall(self, entry_id: str) -> bool:
        """Remove a plugin entry declared in this scope."""

        return self._tree.owner.uninstall(entry_id)

    @property
    def entries(self) -> tuple[str, ...]:
        """Entry ids declared local to this scope, in deterministic order."""

        return self._tree.owner.entries_for_scope(self.path)

    # ------------------------------------------------------------ requirements

    def require(
        self,
        capability: CapabilityKey | str,
        specifier: str = "",
        *,
        optional: bool = False,
    ) -> CapabilityRequirement:
        """Declare a capability requirement *of this scope*.

        A scope requirement is composition intent: it is resolved against the
        scope's visible providers, recorded in provenance, and reported by
        diagnostics. It never gates the activation of plugins -- those have their
        own declarations -- so an unsatisfied scope requirement is something to
        explain, not a reason to remove a plugin.
        """

        name = capability.name if isinstance(capability, CapabilityKey) else capability
        requirement = CapabilityRequirement.parse(name, specifier, optional=optional)
        self._requirements[name] = requirement
        self._tree.invalidate()
        return requirement

    def drop_requirement(self, capability: CapabilityKey | str) -> bool:
        name = capability.name if isinstance(capability, CapabilityKey) else capability
        removed = self._requirements.pop(name, None) is not None
        if removed:
            self._tree.invalidate()
        return removed

    @property
    def requirements(self) -> tuple[CapabilityRequirement, ...]:
        return tuple(self._requirements[name] for name in sorted(self._requirements))

    # ----------------------------------------------------------- capability view

    def restrict(self, *capabilities: CapabilityKey | str) -> None:
        """Narrow the composition this scope exposes (and its descendants inherit).

        Restriction is composition visibility, not authorization: in-process
        plugins remain trusted code. A restricted capability is not merely hidden
        from diagnostics -- consumers in this scope cannot resolve it.
        """

        names = frozenset(
            item.name if isinstance(item, CapabilityKey) else item for item in capabilities
        )
        self._capabilities = names
        self._tree.invalidate()

    def unrestrict(self) -> None:
        """Remove the capability view, exposing everything the parent exposes."""

        self._capabilities = None
        self._tree.invalidate()

    def allows(self, capability: CapabilityKey | str) -> bool:
        """Whether this scope's own view permits a capability (ignoring ancestors)."""

        if self._capabilities is None:
            return True
        name = capability.name if isinstance(capability, CapabilityKey) else capability
        return name in self._capabilities

    # ----------------------------------------------------------------- tool view

    def select_tools(self, *tools: str) -> None:
        """Narrow the tools this scope exposes (and its descendants inherit).

        The set is a ceiling, applied to the scope's own tool-contributing entries
        and to every tool inherited from its ancestors. ``None`` (the default)
        exposes every tool visible from the lineage. This is composition
        visibility: external authorization still decides whether a concrete action
        is allowed.
        """

        self._tools = frozenset(tools)
        self._tree.invalidate()

    def expose_all_tools(self) -> None:
        """Remove the tool view, exposing every tool visible from the lineage."""

        self._tools = None
        self._tree.invalidate()

    def exposes_tool(self, name: str) -> bool:
        """Whether this scope's own view permits a tool (ignoring ancestors)."""

        return self._tools is None or name in self._tools

    # ------------------------------------------------------------------ output

    def to_spec(self) -> ScopeSpec:
        return ScopeSpec(
            path=self.path,
            name=self._name,
            parent=None if self._parent is None else self._parent.path,
            capabilities=self._capabilities,
            tools=self._tools,
            requirements=self.requirements,
            metadata=dict(self._metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        """Structured form. Metadata values are never included."""

        return {
            "path": self.path,
            "name": self._name,
            "parent": None if self._parent is None else self._parent.path,
            "children": [child.path for child in self._children],
            "capabilities": None if self._capabilities is None else sorted(self._capabilities),
            "tools": None if self._tools is None else sorted(self._tools),
            "requirements": [str(item) for item in self.requirements],
            "entries": list(self.entries),
            "metadata_keys": sorted(self._metadata),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CompositionScope(path={self.path!r})"


class CompositionTree:
    """The desired-state tree of composition scopes.

    Owned by a harness, which supplies the desired-state operations (install,
    uninstall, entry lookup) so a scope can declare entries without knowing how
    the control plane stores them.
    """

    __slots__ = ("_owner", "_root", "_scopes")

    def __init__(self, owner: _ScopeOwner | None = None) -> None:
        self._owner = owner
        self._scopes: dict[str, CompositionScope] = {}
        self._root = CompositionScope(self, ROOT_NAME)
        self._scopes[ROOT_PATH] = self._root

    @property
    def owner(self) -> _ScopeOwner:
        if self._owner is None:  # pragma: no cover - defensive
            raise ConfigurationError(
                "this composition tree is not attached to a harness; "
                "create scopes through harness.composition"
            )
        return self._owner

    @property
    def root(self) -> CompositionScope:
        return self._root

    # --------------------------------------------------------------- structure

    def attach(self, scope: CompositionScope) -> CompositionScope:
        """Index a scope created by :meth:`CompositionScope.child`."""

        path = scope.path
        if path in self._scopes:
            raise ConfigurationError("duplicate composition scope path", path=path)
        parent = scope.parent
        assert parent is not None
        parent.add_child(scope)
        self._scopes[path] = scope
        self.invalidate()
        return scope

    def child(
        self,
        name: str,
        *,
        parent: CompositionScope | str | None = None,
        capabilities: Iterable[str | CapabilityKey] | None = None,
        tools: Iterable[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        create_parents: bool = False,
    ) -> CompositionScope:
        """Create a scope, optionally creating missing ancestors of ``parent``."""

        if parent is None:
            parent_scope = self._root
        elif isinstance(parent, str):
            if create_parents:
                parent_scope = self.ensure(parent)
            else:
                existing = self.get(parent)
                if existing is None:
                    raise ConfigurationError("composition scope parent does not exist", path=parent)
                parent_scope = existing
        else:
            parent_scope = parent
        _validate_parent_ownership(self, parent_scope, name)
        return parent_scope.child(name, capabilities=capabilities, tools=tools, metadata=metadata)

    def ensure(self, path: str) -> CompositionScope:
        """Return the scope at ``path``, creating it (and ancestors) when absent.

        ``path`` must be canonical (see
        :func:`~chassis.core.paths.canonical_scope_path`).
        """

        path = canonical_scope_path(path)
        existing = self.get(path)
        if existing is not None:
            return existing
        if path == ROOT_PATH:
            return self._root
        parent_path, _, name = path.rpartition(_PATH_SEPARATOR)
        parent_path = parent_path or ROOT_PATH
        parent = self.ensure(parent_path)
        return parent.child(name)

    def get(self, path: str) -> CompositionScope | None:
        """The scope at ``path``, or ``None``.

        ``path`` must be canonical (see
        :func:`~chassis.core.paths.canonical_scope_path`).
        """

        return self._scopes.get(canonical_scope_path(path))

    def paths(self) -> tuple[str, ...]:
        """Every scope path, in deterministic pre-order."""

        return tuple(scope.path for scope in self._ordered())

    def scopes(self) -> tuple[CompositionScope, ...]:
        return self._ordered()

    def _ordered(self) -> tuple[CompositionScope, ...]:
        ordered: list[CompositionScope] = []

        def walk(scope: CompositionScope) -> None:
            ordered.append(scope)
            for child in sorted(scope.children, key=lambda item: item.name):
                walk(child)

        walk(self._root)
        return tuple(ordered)

    def remove(self, path: str) -> tuple[str, ...]:
        """Remove a scope and its descendants, uninstalling their entries.

        Returns the entry ids that were withdrawn from desired state. Published
        generations are unaffected: a run holding an older generation keeps the
        scope it acquired, and its resources are disposed only when no live
        generation reaches them.
        """

        if path == ROOT_PATH:
            raise ConfigurationError("the root composition scope cannot be removed", path=path)
        path = canonical_scope_path(path)
        scope = self._scopes.get(path)
        if scope is None:
            return ()
        removed_entries: list[str] = []
        for descendant in sorted(
            (item for item in self._ordered() if _is_within(item.path, path)),
            key=lambda item: item.path.count(_PATH_SEPARATOR),
            reverse=True,
        ):
            for entry_id in descendant.entries:
                if self._owner is not None and self._owner.uninstall(entry_id):
                    removed_entries.append(entry_id)
            self._scopes.pop(descendant.path, None)
        parent = scope.parent
        if parent is not None:
            parent.remove_child(scope)
        self.invalidate()
        return tuple(sorted(removed_entries))

    def invalidate(self) -> None:
        """Mark the owner's desired state as changed."""

        if self._owner is not None:
            self._owner.mark_dirty()

    # ------------------------------------------------------------------- specs

    def specs(self) -> tuple[ScopeSpec, ...]:
        return tuple(scope.to_spec() for scope in self._ordered())

    def to_dict(self) -> dict[str, Any]:
        return {"scopes": [scope.to_dict() for scope in self._ordered()]}

    def __len__(self) -> int:
        return len(self._scopes)

    def __iter__(self) -> Iterator[CompositionScope]:
        return iter(self._ordered())


def _is_within(path: str, ancestor: str) -> bool:
    if ancestor == ROOT_PATH:
        return True
    return path == ancestor or path.startswith(f"{ancestor}{_PATH_SEPARATOR}")


@dataclass(frozen=True, slots=True)
class ResolvedScope:
    """Immutable composition of one scope, as published in a generation."""

    path: str
    name: str
    parent: str | None
    children: tuple[str, ...]
    capabilities: tuple[str, ...] | None
    entries: tuple[str, ...]
    instances: tuple[str, ...]
    providers: Mapping[str, tuple[str, ...]]
    inherited: Mapping[str, tuple[str, ...]]
    visible: Mapping[str, tuple[str, ...]]
    requirements: tuple[RequirementResolution, ...]
    provenance: tuple[RequirementResolution, ...]
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    tools: tuple[str, ...] | None = None
    local_tools: tuple[str, ...] = ()
    inherited_tools: tuple[str, ...] = ()
    visible_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("providers", "inherited", "visible", "metadata"):
            value = getattr(self, name)
            if not isinstance(value, MappingProxyType):
                object.__setattr__(self, name, frozen_mapping(value))

    def is_local(self, provider_entry_id: str) -> bool:
        """Whether a provider entry is declared local to this scope."""

        return provider_entry_id in self.entries

    def exposes_tool(self, name: str) -> bool:
        """Whether this scope's tool view permits a tool (ignoring ancestors)."""

        return self.tools is None or name in self.tools

    def to_dict(self) -> dict[str, Any]:
        """Structured form without metadata values, safe for snapshots."""

        return {
            "path": self.path,
            "name": self.name,
            "parent": self.parent,
            "children": list(self.children),
            "capabilities": None if self.capabilities is None else list(self.capabilities),
            "tools": None if self.tools is None else list(self.tools),
            "entries": list(self.entries),
            "instances": list(self.instances),
            "providers": {name: list(ids) for name, ids in sorted(self.providers.items())},
            "inherited": {name: list(ids) for name, ids in sorted(self.inherited.items())},
            "visible": {name: list(ids) for name, ids in sorted(self.visible.items())},
            "local_tools": list(self.local_tools),
            "inherited_tools": list(self.inherited_tools),
            "visible_tools": list(self.visible_tools),
            "requirements": [item.to_dict() for item in self.requirements],
            "provenance": [item.to_dict() for item in self.provenance],
        }


@dataclass(frozen=True, slots=True)
class ScopeTree:
    """Immutable scope tree of one published generation.

    Deeply immutable: every :class:`ResolvedScope` is frozen and its mappings are
    read-only, so a run that acquired a generation can never observe a scope tree
    changing underneath it. A scope-affecting change produces a new generation
    with a new tree.
    """

    root: str
    scopes: tuple[ResolvedScope, ...]

    @classmethod
    def root_only(cls) -> ScopeTree:
        """The tree of a composition with no declared scopes.

        Still a real root: a flat composition is a one-scope hierarchy, so
        diagnostics and diffs never need a special case for "no scopes".
        """

        return cls(root=ROOT_PATH, scopes=(_flat_root(),))

    def get(self, path: str) -> ResolvedScope | None:
        for scope in self.scopes:
            if scope.path == path:
                return scope
        return None

    def paths(self) -> tuple[str, ...]:
        return tuple(scope.path for scope in self.scopes)

    def root_scope(self) -> ResolvedScope:
        scope = self.get(self.root)
        if scope is None:  # pragma: no cover - defensive
            return _flat_root()
        return scope

    def providers_of(self, path: str, capability: str) -> tuple[str, ...]:
        """Visible provider instance ids for a capability in a scope.

        A missing scope is an error, not an empty answer: a valid scope with no
        visible provider and an unknown scope are different facts.
        """

        scope = self.get(path)
        if scope is None:
            raise ConfigurationError("unknown composition scope in this generation", scope=path)
        return scope.visible.get(capability, ())

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "scopes": [scope.to_dict() for scope in self.scopes],
        }

    def fingerprint(self) -> Any:
        """Canonical payload that changes whenever observable scope topology does.

        Used by snapshots: if a scope is added or removed, an entry moves between
        scopes, a capability view narrows, or a requirement resolves to a
        different provider, the fingerprint changes and the generation digest
        reflects it.
        """

        return {
            "root": self.root,
            "scopes": [
                {
                    "path": scope.path,
                    "parent": scope.parent,
                    "children": list(scope.children),
                    "capabilities": (
                        None if scope.capabilities is None else list(scope.capabilities)
                    ),
                    "tools": None if scope.tools is None else list(scope.tools),
                    "entries": list(scope.entries),
                    "providers": {name: list(ids) for name, ids in sorted(scope.providers.items())},
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
                for scope in self.scopes
            ],
        }

    def __len__(self) -> int:
        return len(self.scopes)

    def __iter__(self) -> Iterator[ResolvedScope]:
        return iter(self.scopes)


def _flat_root() -> ResolvedScope:
    return ResolvedScope(
        path=ROOT_PATH,
        name=ROOT_NAME,
        parent=None,
        children=(),
        capabilities=None,
        entries=(),
        instances=(),
        providers={},
        inherited={},
        visible={},
        requirements=(),
        provenance=(),
    )


def build_scope_tree(
    *,
    plan: ResolutionPlan,
    instances: Sequence[PluginInstance],
    registrations: Sequence[CapabilityRegistration],
    tools: Sequence[RegisteredTool] = (),
) -> ScopeTree:
    """Materialise the resolver's scope plan into an immutable, published tree.

    The resolver decides *what* is visible and why; this function binds that
    decision to the instances and registrations actually mounted, so the published
    tree is authoritative about ownership and never depends on live registry state
    to explain itself. ``tools`` are the live tool registrations; each is owned by
    the instance whose scope declared it, so tool visibility follows the same
    inheritance-plus-narrowing rule as capability visibility.
    """

    if not plan.scopes:
        return ScopeTree.root_only()

    instance_by_entry: dict[str, PluginInstance] = {}
    for instance in instances:
        instance_by_entry.setdefault(instance.entry_id, instance)
    registrations_by_instance: dict[str, list[CapabilityRegistration]] = {}
    for registration in registrations:
        registrations_by_instance.setdefault(registration.provider_id, []).append(registration)

    plans: dict[str, ScopePlan] = {scope.path: scope for scope in plan.scopes}

    #: Instance id -> declaring scope path, for attributing an owned tool.
    tool_scope_by_instance: dict[str, str] = {}
    for scope_plan in plan.scopes:
        for entry in scope_plan.entries:
            instance = instance_by_entry.get(entry)
            if instance is not None:
                tool_scope_by_instance[instance.instance_id] = scope_plan.path
    tool_names_by_scope: dict[str, set[str]] = {}
    for tool in tools:
        owner_scope = tool_scope_by_instance.get(tool.owner_id)
        if owner_scope is None:
            continue
        tool_names_by_scope.setdefault(owner_scope, set()).add(tool.name)

    def bind_instance(entry_id: str | None, instance_id: str | None) -> str | None:
        """Resolve a plan's cached provider instance id against what was mounted.

        The resolver computes the plan before anything is mounted, so a provider
        that becomes active in this reconcile still carries ``None`` there. The
        published tree must name the instance that actually backs the entry, or an
        otherwise-identical later reconcile would compare unequal and churn a
        generation for a physical id that is not part of composition identity.
        """

        if entry_id is None:
            return instance_id
        instance = instance_by_entry.get(entry_id)
        return instance_id if instance is None else instance.instance_id

    def bound_provenance(
        items: Sequence[RequirementResolution],
    ) -> tuple[RequirementResolution, ...]:
        bound: list[RequirementResolution] = []
        for item in items:
            bound.append(
                replace(
                    item,
                    provider_instance_id=bind_instance(
                        item.provider_entry_id, item.provider_instance_id
                    ),
                    assessments=tuple(
                        replace(
                            assessment,
                            provider_instance_id=bind_instance(
                                assessment.provider_entry_id, assessment.provider_instance_id
                            ),
                        )
                        for assessment in item.assessments
                    ),
                )
            )
        return tuple(bound)

    def local_instance_ids(path: str) -> tuple[str, ...]:
        scope_plan = plans.get(path)
        if scope_plan is None:
            return ()
        return tuple(
            sorted(
                instance_by_entry[entry].instance_id
                for entry in scope_plan.entries
                if entry in instance_by_entry
            )
        )

    def local_providers(path: str) -> dict[str, tuple[str, ...]]:
        collected: dict[str, set[str]] = {}
        for instance_id in local_instance_ids(path):
            for registration in registrations_by_instance.get(instance_id, ()):
                collected.setdefault(registration.key.name, set()).add(instance_id)
        return {name: tuple(sorted(ids)) for name, ids in sorted(collected.items())}

    def effective_view(path: str) -> frozenset[str] | None:
        """Capability view after intersecting every ancestor's view."""

        result: frozenset[str] | None = None
        current: str | None = path
        while current is not None:
            scope_plan = plans.get(current)
            if scope_plan is None:
                break
            view = scope_plan.capabilities
            if view is not None:
                names = frozenset(view)
                result = names if result is None else result & names
            current = scope_plan.parent
        return result

    def ancestors_of(path: str) -> tuple[str, ...]:
        chain: list[str] = []
        current = plans[path].parent
        while current is not None:
            chain.append(current)
            current = plans[current].parent
        return tuple(chain)

    def effective_tool_view(path: str) -> frozenset[str] | None:
        """Tool view after intersecting every ancestor's view."""

        result: frozenset[str] | None = None
        current: str | None = path
        while current is not None:
            scope_plan = plans.get(current)
            if scope_plan is None:
                break
            view = scope_plan.tools
            if view is not None:
                names = frozenset(view)
                result = names if result is None else result & names
            current = scope_plan.parent
        return result

    def local_tools(path: str) -> frozenset[str]:
        return frozenset(tool_names_by_scope.get(path, ()))

    resolved: list[ResolvedScope] = []
    for scope_plan in plan.scopes:
        view = effective_view(scope_plan.path)
        local = local_providers(scope_plan.path)
        inherited: dict[str, tuple[str, ...]] = {}
        for ancestor in ancestors_of(scope_plan.path):
            for name, ids in local_providers(ancestor).items():
                inherited.setdefault(name, ())
                inherited[name] = tuple(sorted({*inherited[name], *ids}))
        if view is not None:
            local = {name: ids for name, ids in local.items() if name in view}
            inherited = {name: ids for name, ids in inherited.items() if name in view}
        visible = {
            name: tuple(sorted({*local.get(name, ()), *inherited.get(name, ())}))
            for name in sorted({*local, *inherited})
        }
        tool_view = effective_tool_view(scope_plan.path)
        owned_tools = local_tools(scope_plan.path)
        inherited_tool_names: set[str] = set()
        for ancestor in ancestors_of(scope_plan.path):
            inherited_tool_names.update(local_tools(ancestor))
        inherited_tools = frozenset(inherited_tool_names)
        if tool_view is not None:
            owned_tools = frozenset(name for name in owned_tools if name in tool_view)
            inherited_tools = frozenset(name for name in inherited_tools if name in tool_view)
        resolved.append(
            ResolvedScope(
                path=scope_plan.path,
                name=scope_plan.name,
                parent=scope_plan.parent,
                children=scope_plan.children,
                capabilities=scope_plan.capabilities,
                entries=scope_plan.entries,
                instances=local_instance_ids(scope_plan.path),
                providers=local,
                inherited=inherited,
                visible=visible,
                requirements=scope_plan.requirements,
                provenance=bound_provenance(scope_plan.provenance),
                metadata=dict(scope_plan.metadata),
                tools=scope_plan.tools,
                local_tools=tuple(sorted(owned_tools)),
                inherited_tools=tuple(sorted(inherited_tools)),
                visible_tools=tuple(sorted({*owned_tools, *inherited_tools})),
            )
        )

    return ScopeTree(root=plan.scopes[0].path, scopes=tuple(resolved))
