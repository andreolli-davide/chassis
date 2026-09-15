"""The Chassis control plane.

``Harness`` owns desired plugin state, resolves composition, mounts and disposes
plugin instances, and reports diagnostics. It deliberately does **not** execute
agents: execution lives behind the :class:`~chassis.langgraph.runtime.AgentRuntime`
boundary so that the lifecycle kernel stays independent of any graph engine.

Control-plane operations are serialized through one async lock. Data-plane work
(agent runs) must not take that lock; it acquires an immutable runtime generation
instead.

Reconciliation is transactional from the perspective of new work: candidate
plugins are mounted first, and only then are plugins that left the composition
disposed. If a candidate mount fails, everything mounted during that attempt is
rolled back and the previously active composition is left untouched.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from chassis.capabilities.registry import CapabilityRegistration, CapabilityRegistry
from chassis.core.errors import (
    CleanupFailure,
    EffectCleanupError,
    HarnessStateError,
)
from chassis.core.scope import Scope
from chassis.diagnostics import Diagnostics
from chassis.plugins.base import Plugin
from chassis.plugins.lifecycle import PluginInstance, PluginState
from chassis.plugins.registry import PluginEntry, PluginRegistry
from chassis.plugins.resolver import DependencyResolver, ResolutionPlan

__all__ = ["Harness", "HarnessState", "ReconcileResult"]


class HarnessState(StrEnum):
    """Lifecycle state of the harness itself."""

    CREATED = "created"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """What one reconciliation actually changed."""

    plan: ResolutionPlan
    mounted: tuple[str, ...]
    reused: tuple[str, ...]
    disposed: tuple[str, ...]
    failures: tuple[CleanupFailure, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mounted": list(self.mounted),
            "reused": list(self.reused),
            "disposed": list(self.disposed),
            "failures": [failure.to_dict() for failure in self.failures],
            "plan": self.plan.to_dict(),
        }


class Harness:
    """Composition and lifecycle runtime for Chassis plugins."""

    def __init__(
        self,
        *,
        name: str = "harness",
        task_shutdown_timeout: float | None = 5.0,
    ) -> None:
        self._name = name
        self._state = HarnessState.CREATED
        self._scope = Scope(
            name, description="harness", task_shutdown_timeout=task_shutdown_timeout
        )
        self._capability_registry = CapabilityRegistry()
        self._plugin_registry = PluginRegistry(capabilities=self._capability_registry)
        self._resolver = DependencyResolver()
        self._provider_preference: dict[str, str] = {}
        self._plan: ResolutionPlan | None = None
        self._compose_lock = asyncio.Lock()
        self._last_failures: tuple[CleanupFailure, ...] = ()
        self._diagnostics = Diagnostics(self)

    # ------------------------------------------------------------------ identity

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> HarnessState:
        return self._state

    @property
    def scope(self) -> Scope:
        """Scope owning harness-level resources, closed during shutdown."""

        return self._scope

    @property
    def plugin_registry(self) -> PluginRegistry:
        return self._plugin_registry

    @property
    def capability_registry(self) -> CapabilityRegistry:
        return self._capability_registry

    @property
    def diagnostics(self) -> Diagnostics:
        return self._diagnostics

    @property
    def last_cleanup_failures(self) -> tuple[CleanupFailure, ...]:
        """Cleanup failures observed during the most recent reconciliation."""

        return self._last_failures

    # ------------------------------------------------------------ desired state

    def install(
        self,
        plugin: Plugin | type[Plugin],
        *,
        entry_id: str | None = None,
        config: Mapping[str, object] | None = None,
    ) -> str:
        """Add a desired plugin entry and return its stable entry id.

        Accepts a plugin instance or the class produced by :func:`~chassis.plugins.plugin`.
        Desired-state changes take effect on the next :meth:`reconcile`.
        """

        entry = self._plugin_registry.install(plugin, entry_id=entry_id, config=config)
        return entry.entry_id

    def uninstall(self, entry_id: str) -> bool:
        """Remove a desired plugin entry. Takes effect on the next reconcile."""

        return self._plugin_registry.uninstall(entry_id)

    def prefer_provider(self, capability: str, provider_entry_id: str) -> None:
        """Disambiguate a capability that several providers satisfy.

        The preference is keyed by capability name, or ``"<entry id>:<capability>"``
        to disambiguate for one consumer only.
        """

        self._provider_preference[capability] = provider_entry_id

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> ReconcileResult:
        """Reconcile the desired state and begin accepting work."""

        if self._state is HarnessState.RUNNING:
            return ReconcileResult(self.plan(), (), (), ())
        if self._state is HarnessState.STOPPED:
            raise HarnessStateError("harness has been stopped", harness=self._name)
        result = await self.reconcile()
        self._state = HarnessState.RUNNING
        return result

    async def stop(self) -> None:
        """Gracefully dispose every plugin instance and close the harness.

        Idempotent. Cleanup failures never abort the shutdown; they are aggregated
        and raised once everything that could be disposed has been.
        """

        if self._state in (HarnessState.STOPPED, HarnessState.STOPPING):
            return
        self._state = HarnessState.STOPPING
        failures: list[CleanupFailure] = []
        for instance in self._teardown_order():
            await self._dispose_instance(instance, failures)
        try:
            await self._scope.aclose()
        except EffectCleanupError as error:
            failures.extend(error.failures)
        self._state = HarnessState.STOPPED
        self._last_failures = tuple(failures)
        if failures:
            raise EffectCleanupError(self._name, tuple(failures))

    async def reconcile(self) -> ReconcileResult:
        """Apply the desired state, mounting and disposing plugins as needed."""

        if self._state is HarnessState.STOPPED:
            raise HarnessStateError("harness has been stopped", harness=self._name)
        async with self._compose_lock:
            plan = self._resolver.resolve(
                self._plugin_registry.candidates(), prefer=self._provider_preference
            )
            plan.raise_for_cycles()
            result = await self._apply(plan)
            self._plan = plan
            self._last_failures = result.failures
            return result

    # -------------------------------------------------------------- internals

    async def _apply(self, plan: ResolutionPlan) -> ReconcileResult:
        mounted: list[str] = []
        reused: list[str] = []
        disposed: list[str] = []
        failures: list[CleanupFailure] = []

        try:
            for entry_id in plan.activation_order:
                entry = self._plugin_registry.entry(entry_id)
                if entry is None:  # pragma: no cover - defensive
                    continue
                existing = self._plugin_registry.instance(entry_id)
                if existing is not None and existing.state is PluginState.ACTIVE:
                    reused.append(entry_id)
                    continue
                if existing is not None:
                    await self._dispose_instance(existing, failures)
                await self._plugin_registry.mount(entry, self._resolutions_for(plan, entry_id))
                mounted.append(entry_id)
        except BaseException:
            for entry_id in reversed(mounted):
                candidate = self._plugin_registry.instance(entry_id)
                if candidate is not None:
                    await self._dispose_instance(candidate, failures)
            self._last_failures = tuple(failures)
            raise

        for instance in self._teardown_order(plan):
            await self._dispose_instance(instance, failures)
            disposed.append(instance.entry_id)

        return ReconcileResult(
            plan=plan,
            mounted=tuple(mounted),
            reused=tuple(reused),
            disposed=tuple(disposed),
            failures=tuple(failures),
        )

    def _resolutions_for(
        self, plan: ResolutionPlan, entry_id: str
    ) -> Mapping[str, CapabilityRegistration]:
        """Bind each satisfied requirement of ``entry_id`` to a live registration."""

        plan_entry = plan.plan_for(entry_id)
        if plan_entry is None:  # pragma: no cover - defensive
            return {}
        resolved: dict[str, CapabilityRegistration] = {}
        for resolution in plan_entry.requirements:
            if not resolution.satisfied or resolution.provider_entry_id is None:
                continue
            provider = self._plugin_registry.instance(resolution.provider_entry_id)
            if provider is None:
                continue
            registration = next(
                (
                    candidate
                    for candidate in self._capability_registry.by_name(resolution.requirement.name)
                    if candidate.provider_id == provider.instance_id
                ),
                None,
            )
            if registration is not None:
                resolved[resolution.requirement.name] = registration
        return resolved

    def _teardown_order(self, plan: ResolutionPlan | None = None) -> tuple[PluginInstance, ...]:
        """Mounted instances that must be disposed, consumers before providers.

        ``plan`` defaults to the last applied plan; entries that left desired state
        entirely are still disposed because they remain mounted.
        """

        active = {
            instance.instance_id: instance
            for instance in self._plugin_registry.instances()
            if instance.state in (PluginState.ACTIVE, PluginState.FAILED)
        }
        if plan is not None:
            eligible = set(plan.activation_order)
            doomed = {
                instance_id: instance
                for instance_id, instance in active.items()
                if instance.entry_id not in eligible
            }
        else:
            doomed = dict(active)

        edges: set[tuple[str, str]] = set()
        for instance in active.values():
            for registration in instance.resolved.values():
                if registration.provider_id in active:
                    edges.add((registration.provider_id, instance.instance_id))

        # Consumers become ready before the providers they still reach, so the
        # resulting order is already dependency-safe for teardown.
        order: list[str] = []
        remaining = dict(doomed)
        while remaining:
            ready = sorted(
                instance_id
                for instance_id in remaining
                if not any(
                    provider == instance_id and consumer in remaining
                    for provider, consumer in edges
                )
            )
            if not ready:  # defensive: unexpected cycle among live instances
                ready = sorted(remaining)
            for instance_id in ready:
                order.append(instance_id)
                remaining.pop(instance_id)

        return tuple(active[instance_id] for instance_id in order)

    async def _dispose_instance(
        self, instance: PluginInstance, failures: list[CleanupFailure]
    ) -> None:
        try:
            await self._plugin_registry.dispose(instance)
        except EffectCleanupError as error:
            failures.extend(error.failures)
        except Exception as error:
            failures.append(
                CleanupFailure(description=f"dispose of {instance.manifest.name!r}", error=error)
            )

    # ------------------------------------------------------------- dry-run plan

    def plan(self, *, prefer: Mapping[str, str] | None = None) -> ResolutionPlan:
        """Resolve the desired state without applying it."""

        return self._resolver.resolve(
            self._plugin_registry.candidates(),
            prefer=dict(self._provider_preference) if prefer is None else prefer,
        )

    @property
    def applied_plan(self) -> ResolutionPlan | None:
        """Plan produced by the most recent successful reconciliation."""

        return self._plan

    def entry(self, entry_id: str) -> PluginEntry | None:
        return self._plugin_registry.entry(entry_id)

    # -------------------------------------------------------- context manager

    async def __aenter__(self) -> Harness:
        await self.start()
        return self

    async def __aexit__(self, *exc_details: object) -> bool:
        await self.stop()
        return False
