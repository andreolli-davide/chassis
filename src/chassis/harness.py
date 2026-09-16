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
from typing import Any, Self

from chassis.agents import AgentRegistry, environment_budget
from chassis.budget.models import BudgetLimits
from chassis.capabilities.keys import CapabilityKey
from chassis.capabilities.registry import CapabilityRegistration, CapabilityRegistry
from chassis.capabilities.snapshot import CapabilitySnapshot
from chassis.config.loader import PluginCatalog, parse_config
from chassis.config.models import HarnessConfig
from chassis.config.reconcile import (
    DesiredStateAction,
    DesiredStateChange,
    InstalledEntry,
    config_fingerprint,
    diff_desired_state,
)
from chassis.core.errors import (
    CleanupFailure,
    EffectCleanupError,
    HarnessStateError,
)
from chassis.core.generation import RuntimeGeneration
from chassis.core.generations import GenerationManager
from chassis.core.scope import Scope
from chassis.diagnostics import Diagnostics
from chassis.hooks.registry import HookRegistry, HookSnapshot
from chassis.persistence.snapshots import RuntimeSnapshot
from chassis.plugins.base import Plugin, PluginContext, plugin
from chassis.plugins.lifecycle import PluginInstance, PluginState
from chassis.plugins.registry import PluginEntry, PluginRegistry
from chassis.plugins.resolver import DependencyResolver, ResolutionPlan
from chassis.policy.engine import AllowAllPolicy, PolicyEngine
from chassis.runtime import AgentRuntime, RunEnvironment
from chassis.secrets.base import SecretProvider
from chassis.secrets.env import EnvSecretProvider, RedactingSecretProvider
from chassis.secrets.redaction import SecretRedactor
from chassis.telemetry.base import NoopTelemetry, Telemetry
from chassis.tools.executor import ApprovalGate, ToolExecutor
from chassis.tools.registry import ToolRegistry, ToolSnapshot

__all__ = ["ConfigApplyResult", "Harness", "HarnessState", "ReconcileResult"]


@dataclass(frozen=True, slots=True)
class ConfigApplyResult:
    """What applying a declarative configuration decided and did."""

    config: HarnessConfig
    changes: tuple[DesiredStateChange, ...]
    applied: tuple[DesiredStateChange, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.config.version,
            "changes": [change.to_dict() for change in self.changes],
            "applied": [change.to_dict() for change in self.applied],
        }


def _services_plugin(
    provisions: tuple[tuple[CapabilityKey, object, str | None], ...],
) -> type[Plugin]:
    """Build the plugin that exposes application-provided capabilities.

    Modelling provisions as a plugin keeps one implementation of ownership: they
    participate in resolution, are ordered before their consumers, and are
    withdrawn when their scope closes.
    """

    provides = {
        key.name: version if version is not None else key.api_version
        for key, _value, version in provisions
    }

    @plugin(name="chassis-services", version="1.0.0", provides=provides)
    async def services(ctx: PluginContext) -> None:
        for key, value, version in provisions:
            ctx.capabilities.provide(key, value, version=version)

    return services


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
        policy: PolicyEngine | None = None,
        secrets: SecretProvider | None = None,
        approvals: ApprovalGate | None = None,
        telemetry: Telemetry | None = None,
        redactor: SecretRedactor | None = None,
        tool_timeout_seconds: float | None = None,
        default_budget_limits: BudgetLimits | None = None,
    ) -> None:
        self._name = name
        self._state = HarnessState.CREATED
        self._scope = Scope(
            name, description="harness", task_shutdown_timeout=task_shutdown_timeout
        )
        self._capability_registry = CapabilityRegistry()
        self._tool_registry = ToolRegistry()
        self._hook_registry = HookRegistry()
        self._agents = AgentRegistry(harness=self)
        self._plugin_registry = PluginRegistry(
            capabilities=self._capability_registry,
            tools=self._tool_registry,
            hooks=self._hook_registry,
            agents=self._agents,
        )
        self._generations = GenerationManager(history_limit=generation_history_limit)
        self._policy: PolicyEngine = policy if policy is not None else AllowAllPolicy()
        self._telemetry: Telemetry = telemetry if telemetry is not None else NoopTelemetry()
        self._redactor = redactor if redactor is not None else SecretRedactor()
        # Secrets resolved through the harness become redactable at the moment
        # they are read, which is what keeps them out of traces and snapshots.
        self._secrets: SecretProvider = RedactingSecretProvider(
            secrets if secrets is not None else EnvSecretProvider(), self._redactor
        )
        self._tool_executor = ToolExecutor(
            policy=self._policy,
            approvals=approvals,
            hooks=self._hook_registry,
            telemetry=self._telemetry,
            redactor=self._redactor,
            default_timeout_seconds=tool_timeout_seconds,
        )
        self._catalog = PluginCatalog()
        self._provisions: dict[str, tuple[CapabilityKey, object, str | None]] = {}
        self._config: HarnessConfig | None = None
        self._dirty = False
        self._default_budget_limits = default_budget_limits
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
    def tools(self) -> ToolRegistry:
        """Live tool registry; registrations are owned by plugin scopes."""

        return self._tool_registry

    @property
    def hooks(self) -> HookRegistry:
        """Live hook registry; registrations are owned by plugin scopes."""

        return self._hook_registry

    @property
    def catalog(self) -> PluginCatalog:
        """Implementation names used by declarative configuration."""

        return self._catalog

    @property
    def config(self) -> HarnessConfig | None:
        """Configuration applied most recently, if any."""

        return self._config

    def register_plugin_type(
        self, name: str, plugin_type: type[Plugin], *, replace: bool = False
    ) -> None:
        """Register a plugin implementation for declarative installation."""

        self._catalog.register(name, plugin_type, replace=replace)

    def apply_config(self, source: Any) -> ConfigApplyResult:
        """Reconcile the harness towards a declarative configuration.

        Desired entries are created, replaced, or removed through the same install
        path used programmatically, and provider preferences are applied before the
        next reconcile so the composition is resolved exactly once.
        """

        config = parse_config(source)
        changes = diff_desired_state(config, self._installed_state())
        applied: list[DesiredStateChange] = []

        for change in changes:
            if not change.is_mutation:
                continue
            entry = config.entry(change.entry_id)
            if change.action is DesiredStateAction.REMOVE:
                self.uninstall(change.entry_id)
            elif entry is not None:
                self.install(
                    self._catalog.get(entry.plugin),
                    entry_id=entry.id,
                    config=entry.config,
                    replace=change.action is DesiredStateAction.REPLACE,
                )
            applied.append(change)

        for capability, provider in config.provider_preferences.items():
            self.prefer_provider(capability, provider)
        for entry in config.enabled_entries:
            for capability, provider in entry.provider_preference.items():
                self.prefer_provider(f"{entry.id}:{capability}", provider)

        self._config = config
        return ConfigApplyResult(config=config, changes=changes, applied=tuple(applied))

    def _installed_state(self) -> dict[str, InstalledEntry]:
        return {
            entry.entry_id: InstalledEntry(
                entry_id=entry.entry_id,
                plugin=entry.manifest.name,
                revision=entry.revision,
                config_fingerprint=config_fingerprint(
                    plugin=entry.manifest.name, config=entry.config
                ),
            )
            for entry in self._plugin_registry.entries()
        }

    @property
    def agents(self) -> AgentRegistry:
        """Registered agent runtimes and the app-facing invocation surface."""

        return self._agents

    @property
    def tool_executor(self) -> ToolExecutor:
        """The harness-controlled tool execution boundary."""

        return self._tool_executor

    @property
    def policy(self) -> PolicyEngine:
        return self._policy

    @property
    def secrets(self) -> SecretProvider:
        return self._secrets

    @property
    def telemetry(self) -> Telemetry:
        return self._telemetry

    @property
    def redactor(self) -> SecretRedactor:
        return self._redactor

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
        self._dirty = True
        return entry.entry_id

    def provide(
        self,
        capability: CapabilityKey,
        value: object,
        *,
        version: str | None = None,
    ) -> None:
        """Provide a capability the application already holds.

        The value is mounted as a plugin entry owned by the harness, so it takes
        part in dependency resolution exactly like any other provider and is
        withdrawn when the harness stops. Desired-state changes take effect on the
        next :meth:`reconcile`.
        """

        self._provisions[capability.name] = (capability, value, version)
        self._sync_services_entry()
        self._dirty = True

    def withdraw(self, capability: CapabilityKey | str) -> bool:
        """Undo :meth:`provide` for a capability."""

        name = capability.name if isinstance(capability, CapabilityKey) else capability
        if self._provisions.pop(name, None) is None:
            return False
        self._sync_services_entry()
        self._dirty = True
        return True

    def _sync_services_entry(self) -> None:
        entry_id = "chassis.services"
        if not self._provisions:
            self._plugin_registry.uninstall(entry_id)
            return
        services = _services_plugin(tuple(self._provisions.values()))
        self.install(
            services, entry_id=entry_id, replace=entry_id in self._registrations_snapshot()
        )

    def _registrations_snapshot(self) -> set[str]:
        return {entry.entry_id for entry in self._plugin_registry.entries()}

    def uninstall(self, entry_id: str) -> bool:
        """Remove a desired plugin entry. Takes effect on the next reconcile."""

        removed = self._plugin_registry.uninstall(entry_id)
        self._dirty = self._dirty or removed
        return removed

    def prefer_provider(self, capability: str, provider_entry_id: str) -> None:
        """Disambiguate a capability that several providers satisfy.

        Keyed by capability name, or ``"<entry id>:<capability>"`` to disambiguate
        for one consumer only.
        """

        self._provider_preference[capability] = provider_entry_id
        self._dirty = True

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> ReconcileResult:
        """Reconcile the desired state and begin accepting runs.

        Calling ``start`` on a running harness applies any desired-state changes
        made since the last reconciliation, so the method is safely idempotent.
        """

        if self._state is HarnessState.STOPPED:
            raise HarnessStateError("harness has been stopped", harness=self._name)
        if self._state is HarnessState.CREATED:
            self._state = HarnessState.RUNNING
        if self._dirty or self._plan is None:
            return await self.reconcile()
        return ReconcileResult(self.plan(), self._generation_id_or_empty(), (), (), ())

    async def stop(self) -> None:
        """Gracefully drain runs, dispose every plugin instance, and close down.

        Idempotent. Cleanup failures never abort the shutdown; they are aggregated
        and raised once everything that could be disposed has been.
        """

        if self._state in (HarnessState.STOPPED, HarnessState.STOPPING):
            return
        self._state = HarnessState.STOPPING
        failures: list[CleanupFailure] = []
        async with self._telemetry.span("harness.shutdown", {"harness": self._name}):
            await self._shutdown(failures)

        self._state = HarnessState.STOPPED
        self._last_failures = tuple(failures)
        if failures:
            raise EffectCleanupError(self._name, tuple(failures))

    async def _shutdown(self, failures: list[CleanupFailure]) -> None:
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

    @property
    def has_pending_changes(self) -> bool:
        """Whether desired state changed since the last successful reconcile."""

        return self._dirty

    async def ensure_ready(self) -> None:
        """Apply pending desired-state changes before data-plane work begins.

        Programmatic ``install``/``provide``/``uninstall`` are synchronous, so they
        cannot publish a generation themselves. Any asynchronous entry point that
        depends on the composition -- an agent invocation or an explicit reconcile
        -- applies the pending changes first, which keeps the API ergonomic without
        starting unowned background work.
        """

        if self._dirty:
            await self.reconcile()

    async def reconcile(self) -> ReconcileResult:
        """Publish a new runtime generation for the desired state."""

        if self._state is HarnessState.STOPPED:
            raise HarnessStateError("harness has been stopped", harness=self._name)

        async with (
            self._compose_lock,
            self._telemetry.span(
                "harness.reconcile", {"harness": self._name, "desired": len(self.plan().plugins)}
            ) as span,
        ):
            plan = self._resolver.resolve(
                self._plugin_registry.candidates(), prefer=self._provider_preference
            )
            self._telemetry.event(
                "dependency.resolve",
                {
                    "eligible": len(plan.activation_order),
                    "pending": len(plan.pending),
                    "cycles": len(plan.cycles),
                    "edges": len(plan.edges),
                },
            )
            plan.raise_for_cycles()

            failures: list[CleanupFailure] = []
            mounted: list[PluginInstance] = []
            reused: list[str] = []
            try:
                instances = await self._materialize(plan, mounted, reused, failures)
            except BaseException as error:
                span.record_error(error)
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
                self._telemetry.event(
                    "generation.build", {"plugins": len(instances), "mounted": len(mounted)}
                )
                generation = self._generations.build(
                    snapshot_factory=snapshot_factory,
                    instances=instances,
                    metadata={"plugins": len(instances)},
                )
                previous = self._generations.publish(generation)
                self._telemetry.event(
                    "generation.publish",
                    {
                        "generation_id": generation.generation_id,
                        "sequence": generation.sequence,
                        "previous": None if previous is None else previous.generation_id,
                        "plugins": len(instances),
                    },
                )
            disposed = await self._reclaim(failures)
            span.set_attributes(
                {
                    "generation_id": generation.generation_id,
                    "mounted": len(mounted),
                    "reused": len(reused),
                    "disposed": len(disposed),
                    "failures": len(failures),
                }
            )

            self._plan = plan
            self._dirty = False
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

        This is the low-level primitive: it leases *whatever is currently
        published*. Call :meth:`ensure_ready` first (as agent invocation does) when
        desired-state changes may still be pending.
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
            async with self._telemetry.span(
                "plugin.mount",
                {"plugin": entry.manifest.name, "entry_id": entry_id, "revision": entry.revision},
            ) as span:
                instance = await self._plugin_registry.mount(
                    entry, self._resolutions_for(plan, entry_id)
                )
                span.set_attribute("instance_id", instance.instance_id)
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
                self._telemetry.event(
                    "generation.drain",
                    {"generation_id": generation.generation_id, "leases": 0},
                )

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
        async with self._telemetry.span(
            "plugin.unmount",
            {
                "plugin": instance.manifest.name,
                "entry_id": instance.entry_id,
                "instance_id": instance.instance_id,
                "state": instance.state.value,
            },
        ) as span:
            try:
                await self._plugin_registry.dispose(instance)
            except EffectCleanupError as error:
                failures.extend(error.failures)
                span.record_error(error)
            except Exception as error:
                failures.append(
                    CleanupFailure(
                        description=f"dispose of {instance.manifest.name!r}", error=error
                    )
                )
                span.record_error(error)

    def _generation_id_or_empty(self) -> str:
        current = self._generations.current
        return "" if current is None else current.generation_id

    def register_agent(
        self,
        runtime: AgentRuntime,
        *,
        scope: Scope | None = None,
        replace: bool = False,
    ) -> AgentRuntime:
        """Register an agent runtime, owned by ``scope`` when one is given."""

        return self._agents.register(runtime, scope=scope, replace=replace)

    def run_environment(
        self, generation: RuntimeGeneration, *, limits: BudgetLimits | None = None
    ) -> RunEnvironment:
        """Assemble the generation-scoped services a run executes against.

        Everything is derived from ``generation``, so a run can never observe a
        service belonging to a different composition than its capabilities.
        """

        effective_limits = limits if limits is not None else self._default_budget_limits
        return RunEnvironment(
            generation=generation,
            capabilities=generation.snapshot,
            tools=self.tool_snapshot(generation),
            hooks=self.hook_snapshot(generation),
            executor=self._tool_executor,
            policy=self._policy,
            secrets=self._secrets,
            telemetry=self._telemetry,
            redactor=self._redactor,
            budget=environment_budget(effective_limits),
        )

    def snapshot_for(
        self,
        generation: RuntimeGeneration,
        *,
        agent: str | None = None,
        graph_definition_hash: str | None = None,
        prompt_hash: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RuntimeSnapshot:
        """Attributable metadata for one runtime generation.

        Configuration is redacted before it is hashed, so a snapshot explains the
        composition without carrying secret material.
        """

        return RuntimeSnapshot.from_generation(
            generation,
            tools=self.tool_snapshot(generation),
            redactor=self._redactor,
            agent=agent,
            graph_definition_hash=graph_definition_hash,
            prompt_hash=prompt_hash,
            metadata=metadata,
        )

    def run_snapshot(self, run_context: Any) -> RuntimeSnapshot:
        """Snapshot for the generation a run context belongs to.

        The generation is looked up by id so the snapshot describes exactly the
        composition the run acquired.
        """

        for generation in self._generations.all_generations():
            if generation.generation_id == run_context.generation_id:
                return self.snapshot_for(
                    generation, agent=getattr(run_context, "agent", None) or None
                )
        raise HarnessStateError(
            "run context refers to an unknown generation",
            generation_id=run_context.generation_id,
        )

    def tool_snapshot(self, generation: RuntimeGeneration) -> ToolSnapshot:
        """Tools reachable from ``generation``, as an immutable view.

        A run executes against the tools of the generation it acquired: a plugin
        that left the composition stops contributing tools to new runs without
        disturbing runs already in flight.
        """

        return self._tool_registry.snapshot(
            generation.generation_id, owner_ids=generation.instance_ids
        )

    def hook_snapshot(self, generation: RuntimeGeneration) -> HookSnapshot:
        """Hooks reachable from ``generation``, as an immutable view."""

        return self._hook_registry.snapshot(
            generation.generation_id, owner_ids=generation.instance_ids
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

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc_details: object) -> bool:
        await self.stop()
        return False
