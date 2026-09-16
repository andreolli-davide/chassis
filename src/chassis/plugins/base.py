"""Plugin author API.

A plugin is either a :class:`Plugin` subclass or a function decorated with
:func:`plugin`. Both forms use the same lifecycle machinery: the decorator simply
builds a subclass whose ``setup`` delegates to the function.

Everything a plugin creates through :class:`PluginContext` is owned by the
plugin's scope, so plugins do not write matching cleanup code.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from typing import Any, ClassVar, TypeVar

from chassis.agents import AgentRegistry, ScopedAgents
from chassis.capabilities.keys import CapabilityKey
from chassis.capabilities.registry import (
    CapabilityRegistration,
    CapabilityRegistry,
    ScopedCapabilities,
)
from chassis.core.errors import CapabilityNotFound, CapabilityVersionMismatch
from chassis.core.scope import EffectRecord, Scope
from chassis.hooks.registry import HookRegistry, ScopedHooks
from chassis.plugins.manifest import PluginManifest
from chassis.tasks.manager import ScopedTasks
from chassis.tools.registry import ScopedTools, ToolRegistry

__all__ = ["Plugin", "PluginContext", "plugin"]

T = TypeVar("T")

SetupFunction = Callable[["PluginContext"], Awaitable[None]]


class PluginContext:
    """What a plugin may do during ``setup`` and ``teardown``.

    The context is bound to exactly one plugin instance and one scope. It never
    exposes the harness control plane, and it never resolves capabilities from
    "whatever is current": required capabilities are injected when the plugin is
    mounted into a specific composition.
    """

    __slots__ = (
        "_agents",
        "_capabilities",
        "_config",
        "_entry_id",
        "_hooks",
        "_instance_id",
        "_manifest",
        "_registry",
        "_resolved",
        "_scope",
        "_tasks",
        "_tools",
    )

    def __init__(
        self,
        *,
        instance_id: str,
        entry_id: str,
        manifest: PluginManifest,
        config: Mapping[str, Any],
        scope: Scope,
        registry: CapabilityRegistry,
        resolved: Mapping[str, CapabilityRegistration],
        tools: ToolRegistry,
        hooks: HookRegistry,
        agents: AgentRegistry,
    ) -> None:
        self._instance_id = instance_id
        self._entry_id = entry_id
        self._manifest = manifest
        self._config = config
        self._scope = scope
        self._registry = registry
        self._resolved = dict(resolved)
        self._capabilities = ScopedCapabilities(registry, scope, instance_id, manifest.name)
        self._tasks = ScopedTasks(scope)
        self._tools = ScopedTools(tools, scope, instance_id, manifest.name)
        self._hooks = ScopedHooks(hooks, scope, instance_id)
        self._agents = ScopedAgents(agents, scope)

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def entry_id(self) -> str:
        """Stable identity of the desired-state entry that mounted this instance."""

        return self._entry_id

    @property
    def manifest(self) -> PluginManifest:
        return self._manifest

    @property
    def name(self) -> str:
        return self._manifest.name

    @property
    def version(self) -> str:
        return self._manifest.version

    @property
    def config(self) -> Mapping[str, Any]:
        """Plugin configuration as supplied at installation time."""

        return self._config

    @property
    def scope(self) -> Scope:
        """The owning scope. Effects registered here are reverted on unload."""

        return self._scope

    @property
    def capabilities(self) -> ScopedCapabilities:
        """Provider registration bound to this plugin's scope."""

        return self._capabilities

    @property
    def tasks(self) -> ScopedTasks:
        """Task API bound to this plugin's scope."""

        return self._tasks

    @property
    def tools(self) -> ScopedTools:
        """Tool registration bound to this plugin's scope.

        Registration is reversible: closing the plugin scope unregisters the tool,
        so plugins never write matching unregistration code.
        """

        return self._tools

    @property
    def hooks(self) -> ScopedHooks:
        """Hook registration bound to this plugin's scope."""

        return self._hooks

    @property
    def agents(self) -> ScopedAgents:
        """Agent registration bound to this plugin's scope.

        A plugin that provides an agent runtime is unregistered exactly when the
        plugin unloads.
        """

        return self._agents

    def require(self, capability: CapabilityKey | str) -> Any:
        """Return the provider object resolved for a required capability.

        Raises:
            CapabilityNotFound: the capability was not resolved for this instance.
            CapabilityVersionMismatch: the resolved provider is on a different
                contract generation than the requested key.
        """

        name = capability.name if isinstance(capability, CapabilityKey) else capability
        registration = self._resolved.get(name)
        if registration is None:
            raise CapabilityNotFound(
                f"capability {name!r} is not resolved for plugin {self._manifest.name!r}",
                plugin=self._manifest.name,
                instance_id=self._instance_id,
                capability=name,
            )
        if (
            isinstance(capability, CapabilityKey)
            and capability.api_version
            and registration.key.api_version != capability.api_version
        ):
            raise CapabilityVersionMismatch(
                f"capability {name!r} resolved to contract generation "
                f"{registration.key.api_version!r}, expected {capability.api_version!r}",
                plugin=self._manifest.name,
                capability=name,
                resolved=registration.key.api_version,
                expected=capability.api_version,
            )
        return registration.value

    def get(self, capability: CapabilityKey | str) -> Any | None:
        """Return the provider object for an optional capability, or ``None``."""

        name = capability.name if isinstance(capability, CapabilityKey) else capability
        registration = self._resolved.get(name)
        return None if registration is None else registration.value

    def cleanup(
        self,
        description: str,
        func: Callable[..., Any],
        *args: Any,
        kind: str = "effect",
        **kwargs: Any,
    ) -> EffectRecord:
        """Register the inverse of an operation performed during setup."""

        return self._scope.cleanup(description, func, *args, kind=kind, **kwargs)

    def create_task(
        self, coro: Coroutine[Any, Any, T], *, name: str | None = None
    ) -> asyncio.Task[T]:
        """Start a background task owned by this plugin's scope."""

        return self._scope.create_task(coro, name=name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self._instance_id,
            "entry_id": self._entry_id,
            "plugin": self._manifest.name,
            "version": self._manifest.version,
            "scope_id": self._scope.id,
        }


class Plugin(ABC):
    """Base class for plugins.

    Subclasses declare a :class:`PluginManifest` as a class attribute and
    implement :meth:`setup`. Plugins installed from declarative configuration must
    accept a single positional configuration mapping.
    """

    #: Declared by every concrete plugin subclass.
    manifest: ClassVar[PluginManifest]

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self._config: Mapping[str, Any] = dict(config or {})

    @property
    def config(self) -> Mapping[str, Any]:
        return self._config

    @abstractmethod
    async def setup(self, ctx: PluginContext) -> None:
        """Create this plugin's capabilities and register its effects.

        Everything created through ``ctx`` is reverted if setup raises.
        """

    async def teardown(self, ctx: PluginContext) -> None:
        """Optional graceful shutdown, run before the scope unwinds.

        Use this for ordering-sensitive shutdown (flushing, draining). Ordinary
        cleanup belongs in the scope, which is unwound automatically.
        """

        return None


def plugin(
    *,
    name: str,
    version: str,
    provides: Mapping[str, str] | None = None,
    requires: Mapping[str, str] | None = None,
    optional: Mapping[str, str] | None = None,
    permissions: tuple[str, ...] | list[str] = (),
    config_version: int = 1,
    metadata: Mapping[str, Any] | None = None,
) -> Callable[[SetupFunction], type[Plugin]]:
    """Turn an async function into a plugin class.

    Example::

        @plugin(name="web-search", version="1.2.0", requires={"http": ">=1,<2"})
        async def web_search(ctx: PluginContext) -> None:
            ctx.capabilities.provide(TOOLS, build_tools())
    """

    manifest = PluginManifest(
        name=name,
        version=version,
        provides=dict(provides or {}),
        requires=dict(requires or {}),
        optional=dict(optional or {}),
        permissions=tuple(permissions),
        config_version=config_version,
        metadata=dict(metadata or {}),
    )

    def decorate(setup_function: SetupFunction) -> type[Plugin]:
        async def setup(self: Plugin, ctx: PluginContext) -> None:
            await setup_function(ctx)

        namespace: dict[str, Any] = {
            "manifest": manifest,
            "setup": setup,
            "__doc__": setup_function.__doc__,
            "__qualname__": setup_function.__qualname__,
            "__module__": setup_function.__module__,
        }
        return type(setup_function.__name__, (Plugin,), namespace)

    return decorate
