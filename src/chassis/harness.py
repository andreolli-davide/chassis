"""The Chassis control plane.

``Harness`` owns desired plugin state, resolves composition, mounts and disposes
plugin instances, and publishes immutable runtime generations. It deliberately
does **not** execute agents: execution lives behind the
:class:`~chassis.langgraph.runtime.AgentRuntime` boundary so the lifecycle kernel
stays independent of any graph engine.

Control plane vs data plane
---------------------------

Composition -- mounting, rollback, publication, disposal -- is serialized through
one async lock. Agent runs never take that lock: they acquire the current
immutable generation (a lease increment with no ``await``), execute, and release.
Nothing serializes individual model or tool calls.

Reconciliation is transactional
-------------------------------

Candidates are mounted before anything is published. If a candidate mount fails,
everything that attempt mounted is rolled back and the previously published
generation remains current, so no run can observe a partial composition.

Safe unload
-----------

A plugin that leaves the composition is removed from the *next* generation but is
only disposed once no live generation can reach it. Runs that hold an older
generation keep working against the environment they acquired.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from chassis.capabilities.registry import CapabilityRegistration, CapabilityRegistry
from chassis.capabilities.snapshot import CapabilitySnapshot
from chassis.core.errors import (
    CleanupFailure,
    EffectCleanupError,
    HarnessStateError,
)
from chassis.core.generation import RuntimeGeneration
from chassis.core.generations import GenerationManager
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
    generation_id: str
    mounted: tuple[str, ...]
    reused: tuple[str, ...]
    disposed: tuple[str, ...]
    failures: tuple[CleanupFailure, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
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
        shutdown_grace_seconds: float | None = 30.0,
        generation_history_limit: int = 32,
    ) -> None:
        self._name = name
        self._state = HarnessState.CREATED
        self._scope = Scope(
            name, description="harness", task_shutdown_timeout=task_shutdown_timeout
        )
        self._capability_registry = CapabilityRegistry()
        self._plugin_registry = PluginRegistry(capabilities=self._capability_registry)
        self._generations = GenerationManager(history_limit=generation_history_limit)
        self._resolver = DependencyResolver()
        self._provider_preference: dict[str, str] = {}
        self._compose_lock = asyncio.Lock()
        self._plan: ResolutionPlan | None = None
        self._last_failures: tuple[CleanupFailure, ...] = ()
        self._shutdown_grace_seconds = shutdown_grace_seconds
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
    def generation_manager(self) -> GenerationManager:
        return self._generations

    @property
    def current_generation(self) -> RuntimeGeneration | None:
        """The generation new runs acquire."""

        return self._generations.current

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
        replace: bool = False,
    ) -> str:
        """Add a desired plugin entry and return its stable entry id.

        Accepts a plugin instance or the class produced by
        :func:`~chassis.plugins.plugin`. Desired-state changes take effect on the
        next :meth:`reconcile`; ``replace=True`` swaps an existing entry for a new
        revision without disturbing the running instance until then.
        """

        entry = self._plugin_registry.install(
            plugin, entry_id=entry_id, config=config, replace=replace
        )
        return entry.entry_id

    def uninstall(self, entry_id: str) -> bool:
        """Remove a desired plugin entry. Takes effect on the next reconcile."""

        return self._plugin_registry.uninstall(entry_id)

    def prefer_provider(self, capability: str, provider_entry_id: str) -> None:
        """Disambiguate a capability that several providers satisfy.

        Keyed by capability name, or ``"<entry id>:<capability>"`` to disambiguate
        for one consumer only.
        """

        self._provider_preference[capability] = provider_entry_id

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> ReconcileResult:
        """Reconcile the desired state and begin accepting runs."""

        if self._state is HarnessState.RUNNING:
            return ReconcileResult(self.plan(), self._generation_id_or_empty(), (), (), ())
        if self._state is HarnessState.STOPPED:
            raise HarnessStateError("harness has been stopped", harness=self._name)
        result = await self.reconcile()
        self._state = HarnessState.RUNNING
        return result

    async def stop(self) -> None:
        """Gracefully drain runs, dispose every plugin instance, and close down.

        Idempotent. Cleanup failures never abort the shutdown; they are aggregated
        and raised once everything that could be disposed has been.
        """

        if self._state in (HarnessState.STOPPED, HarnessState.STOPPING):
            return
        self._state = HarnessState.STOPPING
        failures: list[CleanupFailure] = []

        idle, busy = await self._generations.drain(
            self._generations.begin_shutdown(), timeout_seconds=self._shutdown_grace_seconds
        )
        for generation in idle:
            self._generations.retire(generation)
        for generation in busy:
            failures.append(
                CleanupFailure(
                    description=f"generation {generation.generation_id}",
                    error=TimeoutError(
                        f"{generation.lease_count} run(s) still active after "
                        f"{self._shutdown_grace_seconds}s; retiring anyway"
                    ),
                )
            )
            self._generations.retire(generation)

        self._generations.refresh_references(self._plugin_registry.instances())
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
        """Publish a new runtime generation for the desired state."""

        if self._state is HarnessState.STOPPED:
            raise HarnessStateError("harness has been stopped", harness=self._name)

        async with self._compose_lock:
            plan = self._resolver.resolve(
                self._plugin_registry.candidates(), prefer=self._provider_preference
            )
            plan.raise_for_cycles()

            failures: list[CleanupFailure] = []
            mounted: list[PluginInstance] = []
            reused: list[str] = []
            try:
                instances = await self._materialize(plan, mounted, reused, failures)
            except BaseException:
                for instance in reversed(mounted):
                    await self._dispose_instance(instance, failures)
                self._last_failures = tuple(failures)
                raise

            snapshot_factory = self._snapshot_factory(instances)
            current = self._generations.current
            if current is not None and self._same_composition(current, instances, snapshot_factory):
                # Nothing changed: never churn generations for a no-op reconcile.
                generation = current
                reused = [instance.entry_id for instance in instances]
            else:
                generation = self._generations.build(
                    snapshot_factory=snapshot_factory,
                    instances=instances,
                    metadata={"plugins": len(instances)},
                )
                self._generations.publish(generation)
            disposed = await self._reclaim(failures)

            self._plan = plan
            self._last_failures = tuple(failures)
            return ReconcileResult(
                plan=plan,
                generation_id=generation.generation_id,
                mounted=tuple(instance.entry_id for instance in mounted),
                reused=tuple(reused),
                disposed=tuple(disposed),
                failures=tuple(failures),
            )

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[RuntimeGeneration]:
        """Acquire the current immutable generation for the duration of a run.

        The lease is taken without touching the control-plane lock, so model and
        tool calls never contend with reconfiguration. Releasing the last lease of
        a draining generation is what lets its unreachable plugins be disposed.
        """

        generation = self._generations.acquire()
        try:
            yield generation
        finally:
            if self._generations.release(generation):
                await self._reclaim_after_drain()

    # -------------------------------------------------------------- internals

    async def _materialize(
        self,
        plan: ResolutionPlan,
        mounted: list[PluginInstance],
        reused: list[str],
        failures: list[CleanupFailure],
    ) -> tuple[PluginInstance, ...]:
        """Mount or reuse one instance per eligible entry, in activation order."""

        ordered: list[PluginInstance] = []
        for entry_id in plan.activation_order:
            entry = self._plugin_registry.entry(entry_id)
            if entry is None:  # pragma: no cover - defensive
                continue
            existing = self._plugin_registry.instance(entry_id)
            if (
                existing is not None
                and existing.state is PluginState.ACTIVE
                and existing.entry_revision == entry.revision
            ):
                ordered.append(existing)
                reused.append(entry_id)
                continue
            if existing is not None and existing.state is PluginState.FAILED:
                # Retry: release the failed instance before mounting a new one.
                await self._dispose_instance(existing, failures)
            instance = await self._plugin_registry.mount(
                entry, self._resolutions_for(plan, entry_id)
            )
            ordered.append(instance)
            mounted.append(instance)
        return tuple(ordered)

    def _resolutions_for(
        self, plan: ResolutionPlan, entry_id: str
    ) -> Mapping[str, CapabilityRegistration]:
        """Bind each satisfied requirement of ``entry_id`` to a live registration.

        The provider is looked up by *entry* and resolved against whichever
        instance currently backs it. Providers are mounted before their consumers,
        so a replaced provider contributes its new registration, never the
        outgoing one.
        """

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

    @staticmethod
    def _same_composition(
        current: RuntimeGeneration,
        instances: tuple[PluginInstance, ...],
        snapshot_factory: Any,
    ) -> bool:
        """Whether the candidate is composition-identical to the current generation."""

        candidate_ids = tuple(instance.instance_id for instance in instances)
        if candidate_ids != current.instance_ids:
            return False
        return snapshot_factory(current.generation_id) == current.snapshot

    def _snapshot_factory(self, instances: tuple[PluginInstance, ...]) -> Any:
        instance_ids = {instance.instance_id for instance in instances}

        def build(generation_id: str) -> CapabilitySnapshot:
            registrations = (
                registration
                for registration in self._capability_registry.registrations()
                if registration.provider_id in instance_ids
            )
            return CapabilitySnapshot.from_registrations(generation_id, registrations)

        return build

    async def _reclaim(self, failures: list[CleanupFailure]) -> list[str]:
        """Retire idle generations and dispose plugins no generation can reach."""

        for generation in self._generations.draining():
            if generation.lease_count == 0:
                self._generations.retire(generation)

        instances = self._plugin_registry.instances()
        self._generations.refresh_references(instances)
        reachable = self._generations.reachable_instance_ids()
        doomed = [
            instance
            for instance in instances
            if instance.state is PluginState.ACTIVE and instance.instance_id not in reachable
        ]

        disposed: list[str] = []
        for instance in self._teardown_order(doomed):
            await self._dispose_instance(instance, failures)
            disposed.append(instance.entry_id)

        self._generations.refresh_references(self._plugin_registry.instances())
        return disposed

    async def _reclaim_after_drain(self) -> None:
        async with self._compose_lock:
            failures: list[CleanupFailure] = []
            await self._reclaim(failures)
            if failures:
                self._last_failures = (*self._last_failures, *failures)

    def _teardown_order(
        self, instances: Iterable[PluginInstance] | None = None
    ) -> tuple[PluginInstance, ...]:
        """Order instances so consumers are disposed before the providers they reach."""

        live = {instance.instance_id: instance for instance in self._plugin_registry.instances()}
        target = (
            dict(live)
            if instances is None
            else {instance.instance_id: instance for instance in instances}
        )

        edges: set[tuple[str, str]] = set()
        for instance in live.values():
            for registration in instance.resolved.values():
                if registration.provider_id in live:
                    edges.add((registration.provider_id, instance.instance_id))

        order: list[str] = []
        remaining = dict(target)
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

        return tuple(target[instance_id] for instance_id in order)

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

    def _generation_id_or_empty(self) -> str:
        current = self._generations.current
        return "" if current is None else current.generation_id

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
