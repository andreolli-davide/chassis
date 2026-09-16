"""Plugin instances: desired entries, mounting, rollback, and disposal.

The registry owns *instances*, not composition. It does not decide which plugins
belong in the current runtime generation -- that is the resolver's job -- and it
does not decide when an instance is unreachable -- that is the generation
manager's job. Its responsibility is the mechanical lifecycle:

- create exactly one scope per instance;
- run ``setup`` and, on failure, roll back every effect it created;
- run ``teardown`` and close the scope on disposal;
- never leave a partially configured instance visible.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from chassis.agents import AgentRegistry
from chassis.capabilities.registry import CapabilityRegistration, CapabilityRegistry
from chassis.core.errors import (
    EffectCleanupError,
    PluginLoadError,
    PluginSetupError,
)
from chassis.core.scope import Scope
from chassis.hooks.registry import HookRegistry
from chassis.plugins.base import Plugin, PluginContext
from chassis.plugins.lifecycle import PluginHealth, PluginInstance, PluginState
from chassis.plugins.manifest import PluginManifest
from chassis.plugins.resolver import PluginCandidate
from chassis.tools.registry import ToolRegistry

__all__ = ["PluginEntry", "PluginRegistry"]


@dataclass(frozen=True, slots=True)
class PluginEntry:
    """A desired plugin: stable identity plus the implementation to mount.

    ``revision`` increases every time the entry is installed. A mounted instance
    records the revision it was created for, so a re-installed entry is never
    mistaken for the instance that is already running.
    """

    entry_id: str
    plugin: Plugin
    manifest: PluginManifest
    config: Mapping[str, object] = field(default_factory=dict)
    revision: int = 1


class PluginRegistry:
    """Tracks desired entries and mounted instances."""

    def __init__(
        self,
        *,
        capabilities: CapabilityRegistry,
        tools: ToolRegistry,
        hooks: HookRegistry,
        agents: AgentRegistry,
    ) -> None:
        self._capabilities = capabilities
        self._tools = tools
        self._hooks = hooks
        self._agents = agents
        self._entries: dict[str, PluginEntry] = {}
        self._instances: dict[str, PluginInstance] = {}
        self._by_entry: dict[str, list[str]] = {}
        self._revisions: dict[str, int] = {}

    # ------------------------------------------------------------- desired state

    def install(
        self,
        plugin: Plugin | type[Plugin],
        *,
        entry_id: str | None = None,
        config: Mapping[str, object] | None = None,
        replace: bool = False,
    ) -> PluginEntry:
        """Register a desired plugin entry.

        Accepts either a plugin instance (``MyPlugin(api_key=...)``) or a plugin
        class (what the :func:`~chassis.plugins.plugin` decorator produces). A class
        is instantiated with the supplied configuration mapping.

        Args:
            plugin: Plugin instance or plugin class.
            entry_id: Stable identity of the desired entry. Defaults to a unique
                name derived from the manifest, so two installs of the same
                implementation stay distinguishable.
            config: Effective configuration, for diagnostics and snapshots.
            replace: Replace an existing desired entry and bump its revision. A
                running instance of the previous revision stays alive until no
                generation can reach it.
        """

        plugin_instance = plugin(config) if isinstance(plugin, type) else plugin
        manifest = getattr(plugin_instance, "manifest", None)
        if not isinstance(manifest, PluginManifest):
            raise PluginLoadError(
                "plugin does not declare a PluginManifest",
                plugin=type(plugin_instance).__name__,
            )
        resolved_entry_id = entry_id or self._default_entry_id(manifest)
        if resolved_entry_id in self._entries and not replace:
            raise PluginLoadError(
                "duplicate plugin entry id", entry_id=resolved_entry_id, plugin=manifest.name
            )
        revision = self._revisions.get(resolved_entry_id, 0) + 1
        self._revisions[resolved_entry_id] = revision
        effective_config = dict(config if config is not None else plugin_instance.config)
        entry = PluginEntry(
            entry_id=resolved_entry_id,
            plugin=plugin_instance,
            manifest=manifest,
            config=effective_config,
            revision=revision,
        )
        self._entries[resolved_entry_id] = entry
        return entry

    def uninstall(self, entry_id: str) -> bool:
        """Remove a desired entry.

        A mounted instance is *not* destroyed here: it stays reachable until the
        next reconciliation decides it is no longer part of the composition.
        """

        return self._entries.pop(entry_id, None) is not None

    def entry(self, entry_id: str) -> PluginEntry | None:
        return self._entries.get(entry_id)

    def entries(self) -> tuple[PluginEntry, ...]:
        return tuple(self._entries[entry_id] for entry_id in sorted(self._entries))

    # ---------------------------------------------------------------- instances

    def instance(self, entry_id: str) -> PluginInstance | None:
        """The newest live instance mounted for this entry, if any."""

        instance_ids = self._by_entry.get(entry_id)
        if not instance_ids:
            return None
        return self._instances.get(instance_ids[-1])

    def instances(self) -> tuple[PluginInstance, ...]:
        return tuple(self._instances[instance_id] for instance_id in sorted(self._instances))

    def candidates(self) -> tuple[PluginCandidate, ...]:
        """Desired entries plus live registrations, for the dependency resolver.

        An instance only counts as *active* when it was mounted for the entry's
        current revision. An instance left over from a replaced revision is on its
        way out, so planning uses what the new entry declares instead of what the
        outgoing instance happens to provide.
        """

        candidates: list[PluginCandidate] = []
        for entry in self.entries():
            instance = self.instance(entry.entry_id)
            active = (
                instance is not None
                and instance.state is PluginState.ACTIVE
                and instance.entry_revision == entry.revision
            )
            registrations = (
                self._registrations_of(instance) if instance is not None and active else ()
            )
            candidates.append(
                PluginCandidate(
                    entry_id=entry.entry_id,
                    manifest=entry.manifest,
                    instance_id=None if instance is None else instance.instance_id,
                    active=active,
                    registrations=registrations,
                )
            )
        return tuple(candidates)

    # -------------------------------------------------------------- lifecycle

    async def mount(
        self,
        entry: PluginEntry,
        resolved: Mapping[str, CapabilityRegistration],
    ) -> PluginInstance:
        """Create the instance scope, run setup, and mark the instance active.

        If setup fails, every effect created during setup is reverted and the
        instance is marked ``FAILED``: no partially configured plugin survives a
        failed mount.
        """

        current = self.instance(entry.entry_id)
        if (
            current is not None
            and current.state is not PluginState.DISPOSED
            and current.entry_revision == entry.revision
        ):
            raise PluginLoadError(
                "plugin entry is already mounted for this revision", entry_id=entry.entry_id
            )
        instance_id = f"plugin_{uuid.uuid4().hex[:12]}"
        scope = Scope(f"plugin:{entry.entry_id}", description=entry.manifest.identity)
        instance = PluginInstance(
            instance_id=instance_id,
            entry_id=entry.entry_id,
            manifest=entry.manifest,
            plugin=entry.plugin,
            scope=scope,
            config=entry.config,
            resolved=resolved,
            entry_revision=entry.revision,
        )
        self._instances[instance_id] = instance
        self._by_entry.setdefault(entry.entry_id, []).append(instance_id)

        context = PluginContext(
            instance_id=instance_id,
            entry_id=entry.entry_id,
            manifest=entry.manifest,
            config=entry.config,
            scope=scope,
            registry=self._capabilities,
            resolved=resolved,
            tools=self._tools,
            hooks=self._hooks,
            agents=self._agents,
        )
        instance.context = context

        instance.transition(PluginState.LOADING)
        try:
            await entry.plugin.setup(context)
        except BaseException as error:
            await self._rollback(instance, error)
            if isinstance(error, Exception):
                raise PluginSetupError(
                    f"plugin {entry.manifest.name!r} failed during setup",
                    plugin=entry.manifest.name,
                    entry_id=entry.entry_id,
                    instance_id=instance_id,
                    error=type(error).__name__,
                ) from error
            raise
        instance.transition(PluginState.ACTIVE)
        instance.health = PluginHealth.HEALTHY
        return instance

    async def dispose(self, instance: PluginInstance) -> None:
        """Run teardown, close the instance scope, and mark the instance disposed.

        Refuses to dispose an instance that is still reachable from a runtime
        generation (invariant I6).
        """

        if instance.state is PluginState.DISPOSED:
            return
        if instance.generation_refs > 0:
            raise PluginLoadError(
                "refusing to dispose a plugin instance reachable from a live generation",
                plugin=instance.manifest.name,
                instance_id=instance.instance_id,
                generation_refs=instance.generation_refs,
            )

        teardown_error: BaseException | None = None
        if instance.state is PluginState.ACTIVE:
            instance.transition(PluginState.UNLOADING)
            context = instance.context
            if context is not None:
                try:
                    await instance.plugin.teardown(context)
                except Exception as error:
                    teardown_error = error
                    instance.health = PluginHealth.UNHEALTHY
                    instance.scope.record_failure(f"teardown of {instance.manifest.name!r}", error)
        elif instance.state is PluginState.PENDING:
            instance.transition(PluginState.DISPOSED)
            await instance.scope.aclose()
            self._forget(instance)
            return

        if instance.state is PluginState.LOADING:
            # A mount that never completed is rolled back by `mount`; disposal is
            # only meaningful once the instance reached ACTIVE or FAILED.
            raise PluginLoadError(
                "cannot dispose a plugin instance while it is loading",
                plugin=instance.manifest.name,
                instance_id=instance.instance_id,
            )

        try:
            await instance.scope.aclose()
        finally:
            if instance.state in (PluginState.UNLOADING, PluginState.FAILED):
                instance.transition(PluginState.DISPOSED)
            self._forget(instance)
        if teardown_error is not None:
            instance.error = teardown_error

    async def _rollback(self, instance: PluginInstance, setup_error: BaseException) -> None:
        try:
            await instance.scope.aclose()
        except EffectCleanupError as cleanup_error:
            instance.scope.record_failure("setup rollback", cleanup_error)
        finally:
            instance.transition(PluginState.FAILED)
            instance.health = PluginHealth.UNHEALTHY
            instance.error = setup_error

    # ---------------------------------------------------------------- helpers

    def _forget(self, instance: PluginInstance) -> None:
        """Drop a physically disposed instance from the registry.

        Ownership has already been transferred to its scope's cleanup, so keeping
        the instance would only allow stale lookups; what was disposed is reported
        by the reconcile result that performed it.
        """

        self._instances.pop(instance.instance_id, None)
        instance_ids = self._by_entry.get(instance.entry_id)
        if instance_ids is not None and instance.instance_id in instance_ids:
            instance_ids.remove(instance.instance_id)
            if not instance_ids:
                del self._by_entry[instance.entry_id]

    def _registrations_of(self, instance: PluginInstance) -> tuple[CapabilityRegistration, ...]:
        return tuple(
            registration
            for registration in self._capabilities.registrations()
            if registration.provider_id == instance.instance_id
        )

    def _default_entry_id(self, manifest: PluginManifest) -> str:
        base = manifest.name
        if base not in self._entries:
            return base
        suffix = 2
        while f"{base}-{suffix}" in self._entries:
            suffix += 1
        return f"{base}-{suffix}"
