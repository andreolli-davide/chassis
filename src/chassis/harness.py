"""The Chassis control plane.

``Harness`` owns desired plugin state, resolves composition, mounts and disposes
plugin instances, and publishes immutable runtime generations. It deliberately does
**not** execute agents: execution lives behind the
:class:`~chassis.runtime.AgentRuntime` boundary so the lifecycle kernel
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
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

from chassis.agents import AgentRegistry, environment_budget
from chassis.budget.models import BudgetLimits
from chassis.capabilities.keys import POLICY, SECRETS, CapabilityKey
from chassis.capabilities.registry import CapabilityRegistration, CapabilityRegistry
from chassis.capabilities.snapshot import CapabilitySnapshot
from chassis.composition import (
    CompositionScope,
    CompositionTree,
    ResolvedScope,
    ScopeTree,
    build_scope_tree,
)
from chassis.config.loader import PluginCatalog, parse_config
from chassis.config.models import HarnessConfig, PluginEntryConfig
from chassis.config.reconcile import (
    DesiredStateAction,
    DesiredStateChange,
    InstalledEntry,
    config_fingerprint,
    diff_desired_state,
)
from chassis.core.collections import freeze
from chassis.core.errors import (
    CapabilityAmbiguous,
    CapabilityVersionMismatch,
    ChassisError,
    CleanupFailure,
    ConfigurationError,
    EffectCleanupError,
    HarnessStateError,
    HookExecutionError,
    PluginContractError,
    PluginLoadError,
    PluginSetupError,
    SecretResolutionError,
)
from chassis.core.generation import RuntimeGeneration
from chassis.core.generations import GenerationManager
from chassis.core.identity import (
    ImpactAnalysis,
    NodeObservation,
    SemanticIdentity,
    analyse_impact,
    build_semantic_identity,
    observations_from,
)
from chassis.core.paths import canonical_scope_path
from chassis.core.scope import Scope
from chassis.diagnostics import Diagnostics
from chassis.hooks.registry import HookRegistry, HookSnapshot
from chassis.hooks.types import HookEvent
from chassis.persistence.formats import DIAGNOSTICS_FORMAT_VERSION
from chassis.persistence.snapshots import RuntimeSnapshot, chassis_version
from chassis.planning import (
    INPUT_REASONS,
    ActionKind,
    Ambiguity,
    GenerationImpact,
    PlanAction,
    PlanResult,
    ReasonCode,
    ValidationFailure,
)
from chassis.plugins.base import Plugin, PluginContext, plugin
from chassis.plugins.lifecycle import PluginInstance, PluginState
from chassis.plugins.manifest import PluginManifest
from chassis.plugins.registry import PluginEntry, PluginRegistry
from chassis.plugins.resolver import (
    DependencyResolver,
    PluginCandidate,
    PluginPlan,
    ResolutionPlan,
)
from chassis.policy.engine import AllowAllPolicy, PolicyEngine, PolicyRequest, PolicyResult
from chassis.replay.session import ReplaySession
from chassis.runtime import AgentRuntime, RunEnvironment
from chassis.secrets.base import SecretProvider, SecretValue
from chassis.secrets.env import EnvSecretProvider, RedactingSecretProvider, redacting_secrets
from chassis.secrets.redaction import SecretRedactor
from chassis.telemetry.base import NoopTelemetry, RedactingTelemetry, SafeTelemetry, Telemetry
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
        """Export document; declares its diagnostics format version."""

        return {
            "format_version": DIAGNOSTICS_FORMAT_VERSION,
            "version": self.config.version,
            "changes": [change.to_dict() for change in self.changes],
            "applied": [change.to_dict() for change in self.applied],
        }


class _FailClosedPolicy:
    """Denying stand-in for an unresolvable policy system requirement.

    Resolution failure must deny; it must never widen back to the configured
    default, which may be permissive.
    """

    def __init__(self, error: ChassisError) -> None:
        self._error = error

    async def evaluate(self, request: PolicyRequest) -> PolicyResult:
        return PolicyResult(
            allowed=False,
            reason=f"policy resolution failed: {self._error.message}",
        )


class _FailClosedSecrets:
    """Denying stand-in for an unresolvable secrets system requirement."""

    def __init__(self, error: ChassisError) -> None:
        self._error = error

    async def get(self, name: str) -> SecretValue:
        raise SecretResolutionError(
            "secret provider resolution failed", secret=name, reason=self._error.message
        ) from self._error

    async def get_optional(self, name: str) -> SecretValue | None:
        raise SecretResolutionError(
            "secret provider resolution failed", secret=name, reason=self._error.message
        ) from self._error


def _retrieve_task_exception(task: asyncio.Task[Any]) -> None:
    """Retrieve (never suppress) a background task failure.

    The shutdown task's result is re-raised to every ``stop()`` caller; this
    callback only prevents a failure no caller awaits from disappearing as an
    unretrieved-task warning.
    """

    if not task.cancelled():
        task.exception()


def preference_for(
    preferences: Mapping[str, str], consumer: str, capability: str, scope: str
) -> str | None:
    """Most specific applicable preference: consumer, then scope, then global."""

    scoped = preferences.get(f"{consumer}:{capability}")
    if scoped is not None:
        return scoped
    by_scope = preferences.get(f"scope:{scope}:{capability}")
    if by_scope is not None:
        return by_scope
    return preferences.get(capability)


def _services_plugin(
    provisions: tuple[tuple[CapabilityKey, object, str | None], ...],
) -> type[Plugin]:
    """Build the plugin that exposes application-provided capabilities.

    Modelling provisions as a plugin keeps one implementation of ownership: they
    participate in resolution, are ordered before their consumers, and are
    withdrawn when their scope closes.
    """

    provides: dict[str, tuple[str, ...]] = {}
    for key, _value, version in provisions:
        promised = version if version is not None else key.api_version
        versions = provides.get(key.name, ())
        if promised not in versions:
            provides[key.name] = (*versions, promised)

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
    impact: ImpactAnalysis | None = None

    def to_dict(self, *, sanitize: Callable[[str], str] | None = None) -> dict[str, Any]:
        """Export document; declares its diagnostics format version."""

        return {
            "format_version": DIAGNOSTICS_FORMAT_VERSION,
            "generation_id": self.generation_id,
            "mounted": list(self.mounted),
            "reused": list(self.reused),
            "disposed": list(self.disposed),
            "failures": [failure.to_dict(sanitize=sanitize) for failure in self.failures],
            "plan": self.plan.to_dict(),
            "impact": None if self.impact is None else self.impact.to_dict(),
        }


class Harness:
    """Composition and lifecycle runtime for Chassis plugins.

    Args:
        config: Declarative desired state (a mapping, a YAML/JSON string, a path, or
            a :class:`~chassis.config.HarnessConfig`), applied once when the harness
            starts. Implementations it names resolve through the catalog, so
            register them with :meth:`register_plugin_type` before starting.
    """

    def __init__(
        self,
        config: Any = None,
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
        replay: ReplaySession | None = None,
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
        self._generations = GenerationManager(history_limit=generation_history_limit)
        self._policy: PolicyEngine = policy if policy is not None else AllowAllPolicy()
        self._redactor = redactor if redactor is not None else SecretRedactor()
        # One redaction boundary for every emitted signal: backends receive
        # scrubbed payloads and are never trusted to remove secrets themselves.
        # The safe wrapper contains backend failures so observability can never
        # break runtime correctness or suppress another backend.
        self._telemetry: Telemetry = SafeTelemetry(
            RedactingTelemetry(
                telemetry if telemetry is not None else NoopTelemetry(), self._redactor
            )
        )
        # Secrets resolved through the harness become redactable at the moment
        # they are read, which is what keeps them out of traces and snapshots.
        self._secrets: SecretProvider = RedactingSecretProvider(
            secrets if secrets is not None else EnvSecretProvider(), self._redactor
        )
        # A recording attached to this harness adopts the harness redactor and
        # is re-scrubbed, so an externally supplied session cannot smuggle
        # secrets past the redaction boundary.
        if replay is not None:
            replay.bind_redactor(self._redactor)
        self._plugin_registry = PluginRegistry(
            capabilities=self._capability_registry,
            tools=self._tool_registry,
            hooks=self._hook_registry,
            agents=self._agents,
            secrets=self._secrets,
            task_shutdown_timeout=task_shutdown_timeout,
        )
        self._tool_executor = ToolExecutor(
            policy=self._policy,
            approvals=approvals,
            hooks=self._hook_registry,
            telemetry=self._telemetry,
            redactor=self._redactor,
            default_timeout_seconds=tool_timeout_seconds,
            replay=replay,
        )
        self._catalog = PluginCatalog()
        self._replay = replay
        self._provisions: dict[CapabilityKey, tuple[CapabilityKey, object, str | None]] = {}
        self._composition = CompositionTree(self)
        self._config: HarnessConfig | None = None
        self._dirty = False
        self._default_budget_limits = default_budget_limits
        self._pending_config = config
        self._resolver = DependencyResolver()
        self._provider_preference: dict[str, str] = {}
        self._config_preferences: dict[str, str] = {}
        self._compose_lock = asyncio.Lock()
        self._plan: ResolutionPlan | None = None
        self._last_failures: tuple[CleanupFailure, ...] = ()
        self._shutdown_task: asyncio.Task[None] | None = None
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
    def composition(self) -> CompositionTree:
        """Desired-state tree of composition scopes.

        Scopes are control-plane desired state: create one with
        ``harness.composition.child("research")``, declare entries with
        ``scope.install(...)``, and they become visible only when the next
        generation is published. A published generation carries an immutable
        resolved copy (:class:`~chassis.composition.ScopeTree`).
        """

        return self._composition

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
        """Reconcile the harness towards a declarative configuration, atomically.

        The complete configuration is parsed, migrated, validated, and
        catalog-resolved before any desired state is mutated; entries,
        config-owned preferences, and the stored config then commit as one
        transaction, and a failure restores the exact previous state.

        Config-owned preferences are replaced wholesale on every application,
        so omitted values do not linger. They live separately from programmatic
        :meth:`prefer_provider` preferences, which take precedence.
        """

        config = parse_config(source)
        self._stage_config(config)
        changes = diff_desired_state(config, self._installed_state())
        applied: list[DesiredStateChange] = []
        undo: list[Callable[[], Any]] = []
        previous_config = self._config
        previous_preferences = dict(self._config_preferences)
        previous_dirty = self._dirty
        try:
            for change in changes:
                if not change.is_mutation:
                    continue
                entry = config.entry(change.entry_id)
                if change.action is DesiredStateAction.REMOVE:
                    undo.append(self.entry_restorer(change.entry_id))
                    self.uninstall(change.entry_id)
                elif entry is not None:
                    undo.append(self.entry_restorer(entry.id))
                    self.install(
                        self._catalog.get(entry.plugin),
                        entry_id=entry.id,
                        config=entry.config,
                        replace=change.action is DesiredStateAction.REPLACE,
                    )
                applied.append(change)

            config_preferences = dict(config.provider_preferences)
            for entry in config.enabled_entries:
                for capability, provider in entry.provider_preference.items():
                    config_preferences[f"{entry.id}:{capability}"] = provider
            self._config_preferences = config_preferences
            self._config = config
        except BaseException:
            for restore in reversed(undo):
                restore()
            self._config_preferences = previous_preferences
            self._config = previous_config
            self._dirty = previous_dirty
            raise
        if config_preferences != previous_preferences:
            self._dirty = True
        return ConfigApplyResult(config=config, changes=changes, applied=tuple(applied))

    def _stage_config(self, config: HarnessConfig) -> None:
        """Validate the complete configuration before any desired state mutates.

        Raises:
            ConfigurationError: an entry names an unregistered plugin
                implementation, or a provider preference is malformed.
        """

        for entry in config.plugins:
            self._catalog.get(entry.plugin)
        preferences = dict(config.provider_preferences)
        for entry in config.enabled_entries:
            preferences.update(entry.provider_preference)
        for capability, provider in preferences.items():
            if not str(capability).strip() or not str(provider).strip():
                raise ConfigurationError(
                    "provider preferences need a non-empty capability and provider",
                    capability=str(capability),
                )

    def _effective_preferences(self) -> dict[str, str]:
        """Preferences for resolution: config-owned plus programmatic overrides.

        Precedence: a programmatic :meth:`prefer_provider` preference overrides
        a config-owned preference with the same key; within the merged set the
        most specific key (consumer, then scope, then global) wins.
        """

        return {**self._config_preferences, **self._provider_preference}

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
    def replay(self) -> ReplaySession | None:
        """Record/replay session, when the harness is running in one.

        Tool calls made through this harness are recorded or answered from the
        recording; policy and budgets still apply either way. Lifecycle, snapshot,
        and interrupt boundaries are recorded as well, so a recording explains its
        own composition.
        """

        return self._replay

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

    @property
    def default_budget_limits(self) -> BudgetLimits:
        """Default per-run limits, or an unlimited budget when none are configured."""

        if self._default_budget_limits is None:
            return BudgetLimits()
        return self._default_budget_limits

    # ------------------------------------------------------------ desired state

    def install(
        self,
        plugin: Plugin | type[Plugin],
        *,
        entry_id: str | None = None,
        config: Mapping[str, object] | None = None,
        replace: bool = False,
        scope: CompositionScope | str | None = None,
    ) -> str:
        """Add a desired plugin entry and return its stable entry id.

        Accepts a plugin instance or the class produced by
        :func:`~chassis.plugins.plugin`. Desired-state changes take effect on the
        next :meth:`reconcile`; ``replace=True`` swaps an existing entry for a new
        revision without disturbing the running instance until then.

        Args:
            scope: Composition scope that declares the entry, either a
                :class:`~chassis.composition.CompositionScope` or its path. The
                scope decides which providers the entry may see. Defaults to the
                root scope.
        """

        path = self._scope_path(scope)
        entry = self._plugin_registry.install(
            plugin, entry_id=entry_id, config=config, replace=replace, scope=path
        )
        self._dirty = True
        return entry.entry_id

    def entries_for_scope(self, path: str) -> tuple[str, ...]:
        """Entry ids declared in one composition scope."""

        return self._plugin_registry.entries_for_scope(path)

    def mark_dirty(self) -> None:
        """Signals that desired composition changed and needs reconciliation."""

        self._dirty = True

    def _scope_path(self, scope: CompositionScope | str | None) -> str:
        """Resolve a scope argument to a validated path."""

        if scope is None:
            return self._composition.root.path
        path = canonical_scope_path(scope.path if isinstance(scope, CompositionScope) else scope)
        if self._composition.get(path) is None:
            raise ConfigurationError(
                "cannot install into an undeclared composition scope", path=path
            )
        return path

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

        self._provisions[capability] = (capability, value, version)
        self._sync_services_entry()
        self._dirty = True

    def withdraw(self, capability: CapabilityKey | str) -> bool:
        """Undo :meth:`provide` for a capability."""

        if isinstance(capability, CapabilityKey):
            removed = self._provisions.pop(capability, None) is not None
        else:
            keys = [key for key in self._provisions if key.name == capability]
            for key in keys:
                del self._provisions[key]
            removed = bool(keys)
        if not removed:
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

    def prefer_provider(
        self,
        capability: str,
        provider_entry_id: str,
        *,
        consumer: str | None = None,
        scope: CompositionScope | str | None = None,
    ) -> None:
        """Disambiguate a capability that several providers satisfy.

        The argument selects how broadly the preference applies, most specific
        first at resolution time:

        - ``consumer``: only for that entry's requirements
          (``"<entry id>:<capability>"``);
        - ``scope``: only for requirements resolved in that composition scope
          (``"scope:<path>:<capability>"``);
        - neither: for every consumer of the capability.

        A preference never hides an ambiguity that has no preference: without one,
        a requirement satisfied by several visible providers stays ambiguous and
        its consumer stays pending.

        Programmatic preferences are stored separately from declarative
        configuration preferences and take precedence over them for the same key.
        """

        if consumer is not None and scope is not None:
            raise ConfigurationError(
                "a provider preference cannot be scoped to a consumer and a scope at once",
                consumer=consumer,
            )
        if consumer is not None:
            key = f"{consumer}:{capability}"
        elif scope is not None:
            key = f"scope:{self._scope_path(scope)}:{capability}"
        else:
            key = capability
        self._provider_preference[key] = provider_entry_id
        self._dirty = True

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> ReconcileResult:
        """Reconcile the desired state and begin accepting runs.

        Calling ``start`` on a running harness applies any desired-state changes
        made since the last reconciliation, so the method is safely idempotent. A
        declarative configuration handed to the constructor is applied here, once,
        before the first reconciliation.
        """

        if self._state in (HarnessState.STOPPING, HarnessState.STOPPED):
            raise HarnessStateError(
                f"cannot start a harness that is {self._state.value}",
                harness=self._name,
                state=self._state.value,
            )
        if self._pending_config is not None:
            pending, self._pending_config = self._pending_config, None
            self.apply_config(pending)
        if self._state is HarnessState.CREATED:
            self._state = HarnessState.RUNNING
        if self._dirty or self._plan is None:
            return await self.reconcile()
        return ReconcileResult(self.plan(), self._generation_id_or_empty(), (), (), ())

    async def stop(self) -> None:
        """Gracefully drain runs, dispose every plugin instance, and close down.

        Idempotent and a **barrier**: every caller blocks until the shutdown has
        actually finished and then observes its result — no caller is told the
        harness is stopped while disposal is still running. Cancelling one
        caller does not abort the shutdown: it completes in the background and a
        later ``stop()`` observes the same result. Cleanup failures never abort
        the shutdown; they are aggregated and raised once everything that could
        be disposed has been. Shutdown is terminal: a stopped harness is never
        started again (build a new one), so its terminal state is deterministic.
        """

        task = self._shutdown_task
        if task is None:
            if self._state is HarnessState.STOPPED:
                return
            # Stop accepting runs *before* this call yields: the state flips
            # synchronously, so nothing scheduled behind stop() — even a task
            # already in the ready queue — can acquire a generation.
            self._state = HarnessState.STOPPING
            task = asyncio.ensure_future(self._run_shutdown())
            self._shutdown_task = task
            # Retrieve (not suppress) a failure no caller may observe again, so
            # it cannot vanish as an unretrieved-task warning.
            task.add_done_callback(_retrieve_task_exception)
        await asyncio.shield(task)

    async def _run_shutdown(self) -> None:
        """The one shutdown run: serialised against composition, always terminal."""

        failures: list[CleanupFailure] = []
        try:
            # Holding the composition lock means a reconciliation already in
            # flight finishes before shutdown starts, and a later one cannot
            # publish a generation into a harness that is going away.
            async with self._compose_lock:
                self._state = HarnessState.STOPPING
                async with self._telemetry.span("harness.shutdown", {"harness": self._name}):
                    await self._shutdown(failures)
        except BaseException as error:
            # Never leave the harness wedged in STOPPING: record what failed and
            # reach the terminal state, so the failure is observable instead of
            # the harness silently refusing everything forever.
            failures.append(CleanupFailure(description="shutdown", error=error))
            self._last_failures = tuple(failures)
            self._state = HarnessState.STOPPED
            raise
        self._state = HarnessState.STOPPED
        self._last_failures = tuple(failures)
        if failures:
            raise EffectCleanupError(self._name, tuple(failures), sanitize=self._redactor.redact)

    async def _shutdown(self, failures: list[CleanupFailure]) -> None:
        current = self._generations.current
        if current is not None:
            self._record_lifecycle(
                "harness.shutdown",
                {"generation_id": current.generation_id, "leases": current.lease_count},
            )
            failures.extend(
                await self._observe(
                    HookEvent.GENERATION_DRAINING,
                    {
                        "generation_id": current.generation_id,
                        "sequence": current.sequence,
                        "successor": None,
                        "leases": current.lease_count,
                        "shutdown": True,
                    },
                )
            )
        idle, busy = await self._generations.drain(
            self._generations.begin_shutdown(), timeout_seconds=self._shutdown_grace_seconds
        )
        for generation in idle:
            self._generations.retire(generation)
            self._telemetry.event(
                "generation.retired",
                {"generation_id": generation.generation_id, "leases": 0},
            )
        for generation in busy:
            failure = CleanupFailure(
                description=f"generation {generation.generation_id}",
                error=TimeoutError(
                    f"{generation.lease_count} run(s) still active after "
                    f"{self._shutdown_grace_seconds}s; retiring anyway"
                ),
            )
            failures.append(failure)
            self._emit_cleanup_failure(failure)
            self._generations.retire(generation)
            self._telemetry.event(
                "generation.retired",
                {
                    "generation_id": generation.generation_id,
                    "leases": generation.lease_count,
                },
            )

        self._generations.refresh_references(self._plugin_registry.instances())
        for instance in self._teardown_order():
            try:
                await self._dispose_instance(instance, failures)
            except BaseException as error:
                # Last-resort net: no single instance may abort the shutdown's
                # teardown loop and leak the instances behind it.
                failure = CleanupFailure(
                    description=f"dispose of {instance.manifest.name!r}", error=error
                )
                failures.append(failure)
                self._emit_cleanup_failure(
                    failure, entry_id=instance.entry_id, scope=instance.scope.name
                )

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

        This does not start the harness: invoking before ``start()`` is refused,
        because a harness that was never started has published no generation and
        owns nothing.
        """

        if self._dirty:
            await self.reconcile()

    def _record_lifecycle(self, event: str, payload: Mapping[str, Any] | None = None) -> None:
        """Append a selected lifecycle event to the recording, when one is attached."""

        if self._replay is not None:
            self._replay.record_lifecycle(event, payload)

    def _emit_cleanup_failure(
        self,
        failure: CleanupFailure,
        *,
        entry_id: str | None = None,
        scope: str | None = None,
    ) -> None:
        """Surface one aggregated cleanup failure as a structured signal."""

        self._telemetry.event(
            "cleanup.failure",
            {
                "error_type": type(failure.error).__name__,
                "entry_id": entry_id,
                "scope": scope,
                "description": failure.description,
            },
        )

    async def _observe(
        self, event: HookEvent, payload: Mapping[str, Any]
    ) -> tuple[CleanupFailure, ...]:
        """Dispatch a control-plane hook event and return any handler failures.

        Control-plane hooks are observation points: their failures are recorded and
        aggregated, never allowed to abort a lifecycle transition, because aborting
        one would leak the resource the transition was about to release. The
        ``raise`` error policy therefore downgrades to ``record`` on these
        boundaries.
        """

        try:
            result = await self._hook_registry.dispatch(event, payload)
        except HookExecutionError as error:
            return (CleanupFailure(description=f"hook {event.value}", error=error),)
        return result.failures

    def _require_composable(self) -> None:
        """Refuse a control-plane mutation unless the harness can still compose.

        Checked both before the composition lock (fast refusal) and after acquiring
        it, because a shutdown may have queued ahead of the caller and completed
        meanwhile.
        """

        if self._state not in (HarnessState.CREATED, HarnessState.RUNNING):
            raise HarnessStateError(
                f"cannot reconcile a harness that is {self._state.value}",
                harness=self._name,
                state=self._state.value,
            )

    async def reconcile(self) -> ReconcileResult:
        """Publish a new runtime generation for the desired state."""

        # Refuse before touching the lock, then again once it is held: a
        # reconciliation queued behind a shutdown would otherwise publish a
        # generation into a harness that is going away.
        self._require_composable()

        async with (
            self._compose_lock,
            self._telemetry.span(
                "harness.reconcile",
                {"harness": self._name, "desired": len(self._plugin_registry.entries())},
            ) as span,
        ):
            self._require_composable()

            plan = self._resolver.resolve(
                self._plugin_registry.candidates(),
                prefer=self._effective_preferences(),
                scopes=self._composition.specs(),
            )
            self._telemetry.event(
                "dependency.resolve",
                {
                    "eligible": len(plan.activation_order),
                    "pending": len(plan.pending),
                    "cycles": len(plan.cycles),
                    "edges": len(plan.edges),
                    "scopes": len(plan.scopes),
                },
            )
            plan.raise_for_cycles()

            failures: list[CleanupFailure] = []
            mounted: list[PluginInstance] = []
            reused: list[str] = []
            scope_tree: ScopeTree
            snapshot_factory: Any
            current: RuntimeGeneration | None
            reuse_current = False
            try:
                instances = await self._materialize(plan, mounted, reused, failures)
                self._validate_publication(plan, instances)
                # Everything up to and including the candidate build stays
                # transactional: a failure here rolls the mounted candidate back
                # and leaves the published generation untouched, so no instance
                # can survive referenced by no generation.
                scope_tree = build_scope_tree(
                    plan=plan,
                    instances=instances,
                    registrations=self._capability_registry.registrations(),
                    tools=self._tool_registry.entries(),
                )
                snapshot_factory = self._snapshot_factory(instances)
                current = self._generations.current
                reuse_current = current is not None and self._same_composition(
                    current, instances, snapshot_factory, scope_tree
                )
            except BaseException as error:
                span.record_error(error)
                rolled_back = [instance.entry_id for instance in reversed(mounted)]
                for instance in reversed(mounted):
                    await self._dispose_instance(instance, failures)
                if isinstance(error, PluginContractError):
                    error.context["rolled_back"] = rolled_back
                if isinstance(error, PluginSetupError):
                    # Setup and rollback failures are aggregated here, never
                    # hidden inside the failed scope.
                    failures.extend(error.cleanup_failures)
                self._last_failures = tuple(failures)
                raise

            if reuse_current and current is not None:
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
                    metadata={"plugins": len(instances), "scopes": len(scope_tree)},
                    scopes=scope_tree,
                )
                previous = self._generations.publish(generation)
                if previous is not None:
                    self._telemetry.event(
                        "generation.draining",
                        {
                            "generation_id": previous.generation_id,
                            "successor": generation.generation_id,
                            "leases": previous.lease_count,
                        },
                    )
                    self._record_lifecycle(
                        "generation.draining",
                        {
                            "generation_id": previous.generation_id,
                            "successor": generation.generation_id,
                        },
                    )
                    failures.extend(
                        await self._observe(
                            HookEvent.GENERATION_DRAINING,
                            {
                                "generation_id": previous.generation_id,
                                "sequence": previous.sequence,
                                "successor": generation.generation_id,
                                "leases": previous.lease_count,
                            },
                        )
                    )
                self._record_lifecycle(
                    "generation.publish",
                    {
                        "generation_id": generation.generation_id,
                        "sequence": generation.sequence,
                        "previous": None if previous is None else previous.generation_id,
                        "plugins": len(instances),
                    },
                )
                if self._replay is not None:
                    self._replay.record_snapshot(
                        self.snapshot_for(generation).to_dict(),
                    )
                failures.extend(
                    await self._observe(
                        HookEvent.GENERATION_PUBLISHED,
                        {
                            "generation_id": generation.generation_id,
                            "sequence": generation.sequence,
                            "previous": None if previous is None else previous.generation_id,
                            "plugins": len(instances),
                        },
                    )
                )
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
            impact = self._build_impact(current, scope_tree, instances, generation.generation_id)
            self._telemetry.event("generation.impact", dict(impact.counts()))
            span.set_attributes(
                {
                    "generation_id": generation.generation_id,
                    "mounted": len(mounted),
                    "reused": len(reused),
                    "disposed": len(disposed),
                    "failures": len(failures),
                    "rebuilt": len(impact.rebuilt) + len(impact.rewired),
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
                impact=impact,
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

        self._require_acquirable()
        lease = self._generations.acquire_lease()
        self._telemetry.event(
            "generation.acquire",
            {
                "generation_id": lease.generation.generation_id,
                "sequence": lease.generation.sequence,
            },
        )
        try:
            yield lease.generation
        finally:
            became_idle = self._generations.release_lease(lease)
            self._telemetry.event(
                "generation.release",
                {
                    "generation_id": lease.generation.generation_id,
                    "leases": lease.generation.lease_count,
                },
            )
            if became_idle:
                await self._reclaim_after_drain()

    def _require_acquirable(self) -> None:
        """Refuse new runs once shutdown has begun.

        Shutdown stops accepting new work first and drains afterwards: a run
        acquired while the harness is stopping would be torn down underneath
        itself, so the acquisition boundary refuses it deterministically.
        """

        if self._state in (HarnessState.STOPPING, HarnessState.STOPPED):
            raise HarnessStateError(
                f"cannot start a run on a harness that is {self._state.value}",
                harness=self._name,
                state=self._state.value,
            )

    # -------------------------------------------------------------- internals

    async def _materialize(
        self,
        plan: ResolutionPlan,
        mounted: list[PluginInstance],
        reused: list[str],
        failures: list[CleanupFailure],
    ) -> tuple[PluginInstance, ...]:
        """Mount or reuse one instance per eligible entry, in activation order.

        Reuse is decided by semantic identity, not by revision alone: the exact
        runtime instance is carried into the new generation only when its
        implementation, contracts, configuration, scope, and resolved dependency
        bindings are all unchanged. A changed provider therefore rebuilds its
        consumers too, instead of leaving them bound to a registration that is on
        its way out.
        """

        ordered: list[PluginInstance] = []
        identities: dict[str, SemanticIdentity] = {}
        for entry_id in plan.activation_order:
            entry = self._plugin_registry.entry(entry_id)
            if entry is None:  # pragma: no cover - defensive
                continue
            existing = self._plugin_registry.instance(entry_id)
            identity = self._semantic_identity(entry, plan, identities)
            if (
                existing is not None
                and existing.state is PluginState.ACTIVE
                and existing.entry_revision == entry.revision
                and existing.semantic_identity == identity
            ):
                # Safe reuse: every semantic input is unchanged, so the runtime
                # instance may be shared with the new generation verbatim.
                ordered.append(existing)
                reused.append(entry_id)
                identities[entry_id] = identity
                continue
            if existing is not None and existing.state is PluginState.FAILED:
                # Retry: release the failed instance before mounting a new one.
                await self._dispose_instance(existing, failures)
            async with self._telemetry.span(
                "plugin.mount",
                {"plugin": entry.manifest.name, "entry_id": entry_id, "revision": entry.revision},
            ) as span:
                failures.extend(
                    await self._observe(
                        HookEvent.PLUGIN_MOUNTING,
                        {
                            "plugin": entry.manifest.name,
                            "entry_id": entry_id,
                            "revision": entry.revision,
                            "requirements": sorted(entry.manifest.requires),
                        },
                    )
                )
                instance = await self._plugin_registry.mount(
                    entry,
                    self._resolutions_for(plan, entry_id),
                    supersede=existing is not None,
                )
                instance.semantic_identity = identity
                identities[entry_id] = identity
                span.set_attribute("instance_id", instance.instance_id)
                self._record_lifecycle(
                    "plugin.mount",
                    {
                        "plugin": entry.manifest.name,
                        "entry_id": entry_id,
                        "instance_id": instance.instance_id,
                    },
                )
                failures.extend(
                    await self._observe(
                        HookEvent.PLUGIN_MOUNTED,
                        {
                            "plugin": entry.manifest.name,
                            "entry_id": entry_id,
                            "instance_id": instance.instance_id,
                            "scope_id": instance.scope.id,
                        },
                    )
                )
            ordered.append(instance)
            mounted.append(instance)
        return tuple(ordered)

    def _validate_publication(
        self, plan: ResolutionPlan, instances: tuple[PluginInstance, ...]
    ) -> None:
        """Validate effective registrations before a candidate is published.

        The registry -- not the manifest -- is authoritative about what a plugin
        actually provides, so the resolution fixpoint is checked against actual
        registrations after mount: every promised contract must be registered by
        the instance that promised it, and every satisfied requirement must be
        answerable by its selected provider's registrations. Violations raise
        with a structured diagnostic; the caller rolls the candidate back and
        records the rollback result.
        """

        by_provider: dict[str, list[Any]] = {}
        for registration in self._capability_registry.registrations():
            by_provider.setdefault(registration.provider_id, []).append(registration)

        for instance in instances:
            actual = by_provider.get(instance.instance_id, [])
            actual_keys = {registration.key for registration in actual}
            for name, promised in instance.manifest.provided_contracts():
                key = CapabilityKey.from_version(name, promised)
                if key in actual_keys:
                    continue
                raise PluginContractError(
                    f"plugin {instance.manifest.name!r} promised {key} "
                    "but its registrations do not provide it",
                    provider=instance.entry_id,
                    provider_name=instance.manifest.name,
                    promised=str(key),
                    actual=[f"{item.key} {item.version}" for item in actual],
                    consumers=self._consumers_of(plan, name),
                )

        # Duplicate tool names within one resolved generation are rejected
        # here: across generations, same-named registrations coexist and are
        # selected by owner identity.
        seen_tools: dict[str, str] = {}
        for instance in instances:
            owned = self._tool_registry.snapshot("", owner_ids=[instance.instance_id])
            for entry in owned.entries:
                if entry.name in seen_tools:
                    raise ConfigurationError(
                        "a tool with this name is already registered",
                        tool=entry.name,
                        owner=entry.owner_name,
                    )
                seen_tools[entry.name] = instance.instance_id

        for entry_id in plan.activation_order:
            planned = plan.plan_for(entry_id)
            if planned is None:  # pragma: no cover - defensive
                continue
            for resolution in planned.requirements:
                if not resolution.satisfied:
                    continue
                provider = next(
                    (item for item in instances if item.entry_id == resolution.provider_entry_id),
                    None,
                )
                actual = [] if provider is None else by_provider.get(provider.instance_id, [])
                if any(resolution.requirement.accepts(item.key, item.version) for item in actual):
                    continue
                raise PluginContractError(
                    f"consumer {entry_id!r} resolved {resolution.requirement} to "
                    f"{resolution.provider_entry_id!r}, whose registrations cannot satisfy it",
                    provider=resolution.provider_entry_id,
                    provider_name=resolution.provider_name,
                    promised=str(resolution.requirement),
                    actual=[f"{item.key} {item.version}" for item in actual],
                    consumers=[entry_id],
                )

    def _consumers_of(self, plan: ResolutionPlan, capability: str) -> list[str]:
        """Entry ids whose satisfied requirements depend on ``capability``."""

        consumers: list[str] = []
        for entry_id in plan.activation_order:
            planned = plan.plan_for(entry_id)
            if planned is None:  # pragma: no cover - defensive
                continue
            if any(
                resolution.satisfied and resolution.requirement.name == capability
                for resolution in planned.requirements
            ):
                consumers.append(entry_id)
        return consumers

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
            matching = [
                candidate
                for candidate in self._capability_registry.by_name(resolution.requirement.name)
                if candidate.provider_id == provider.instance_id
                and resolution.requirement.accepts(candidate.key, candidate.version)
            ]
            if not matching:
                continue
            exact = [
                candidate
                for candidate in matching
                if str(candidate.key) == resolution.provider_key
                and str(candidate.version) == resolution.provider_version
            ]
            resolved[resolution.requirement.name] = (exact or matching)[0]
        return resolved

    def _semantic_identity(
        self,
        entry: PluginEntry,
        plan: ResolutionPlan,
        identities: Mapping[str, SemanticIdentity],
        *,
        preferences: Mapping[str, str] | None = None,
        provider_instance: Callable[[str], str | None] | None = None,
    ) -> SemanticIdentity:
        """Compute the semantic identity of one entry in a candidate composition.

        Dependency bindings are read from live control-plane state rather than
        from the plan: providers are materialized before their consumers, so this
        sees the instance the candidate will actually publish.

        ``preferences`` and ``provider_instance`` default to the live control
        plane; preview passes the state a not-yet-applied plan would have.
        """

        registry = self._plugin_registry
        effective_preferences = (
            self._effective_preferences() if preferences is None else dict(preferences)
        )

        def live_provider_instance(provider_entry: str) -> str | None:
            instance = registry.instance(provider_entry)
            return None if instance is None else instance.instance_id

        resolve_instance = provider_instance or live_provider_instance

        def provider_identity(provider_entry: str) -> str | None:
            known = identities.get(provider_entry)
            if known is not None:
                return known.identity_digest()
            instance = registry.instance(provider_entry)
            if instance is not None and instance.semantic_identity is not None:
                return instance.semantic_identity.identity_digest()
            return None

        def provider_display(provider_entry: str) -> str | None:
            known = identities.get(provider_entry)
            if known is not None:
                return known.semantic_id
            instance = registry.instance(provider_entry)
            if instance is not None and instance.semantic_identity is not None:
                return instance.semantic_identity.semantic_id
            return None

        plan_entry = plan.plan_for(entry.entry_id)
        resolutions = () if plan_entry is None else plan_entry.requirements
        plugin_type = type(entry.plugin)
        return build_semantic_identity(
            entry_id=entry.entry_id,
            scope_path=entry.scope,
            manifest=entry.manifest,
            config=entry.config,
            resolutions=resolutions,
            provider_instance=resolve_instance,
            provider_identity=provider_identity,
            provider_display=provider_display,
            preference=lambda consumer, capability, scope: preference_for(
                effective_preferences, consumer, capability, scope
            ),
            implementation_hint=f"{plugin_type.__module__}.{plugin_type.__qualname__}",
            redactor=self._redactor,
        )

    @staticmethod
    def _build_impact(
        current: RuntimeGeneration | None,
        scope_tree: ScopeTree,
        instances: tuple[PluginInstance, ...],
        new_generation_id: str,
    ) -> ImpactAnalysis:
        """Compare the candidate composition against the one being replaced."""

        def view(scopes: ScopeTree) -> Any:
            def lookup(path: str) -> tuple[str, ...] | None:
                resolved = scopes.get(path)
                return None if resolved is None else resolved.capabilities

            return lookup

        candidate = observations_from(instances, scope_view=view(scope_tree))
        if current is None:
            previous: dict[str, NodeObservation] = {}
            old_id: str | None = None
        else:
            previous = observations_from(current.instances, scope_view=view(current.scopes))
            old_id = current.generation_id
        return analyse_impact(
            previous,
            candidate,
            old_generation_id=old_id,
            new_generation_id=new_generation_id,
        )

    @staticmethod
    def _same_composition(
        current: RuntimeGeneration,
        instances: tuple[PluginInstance, ...],
        snapshot_factory: Any,
        scope_tree: ScopeTree,
    ) -> bool:
        """Whether the candidate is composition-identical to the current generation.

        Scope topology is part of composition identity: adding an empty child scope
        or narrowing a capability view is observable through the generation a run
        acquires, so it must publish a new generation even when the mounted
        instances are unchanged.
        """

        candidate_ids = tuple(instance.instance_id for instance in instances)
        if candidate_ids != current.instance_ids:
            return False
        # Observable composition only: topology, selection, and scope metadata.
        # Comparing resolution provenance object-wise would see pre-mount
        # instance ids and churn a generation for an identical composition.
        if not scope_tree.same_observable_composition(current.scopes):
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
                self._record_lifecycle(
                    "generation.retired", {"generation_id": generation.generation_id}
                )
                self._telemetry.event(
                    "generation.retired",
                    {"generation_id": generation.generation_id, "leases": 0},
                )

        instances = self._plugin_registry.instances()
        self._generations.refresh_references(instances)
        reachable = self._generations.reachable_instance_ids()
        desired = {entry.entry_id for entry in self._plugin_registry.entries()}
        doomed = [
            instance
            for instance in instances
            if (instance.state is PluginState.ACTIVE and instance.instance_id not in reachable)
            or (instance.state is PluginState.FAILED and instance.entry_id not in desired)
        ]

        disposed: list[str] = []
        for instance in self._teardown_order(doomed):
            await self._dispose_instance(instance, failures)
            disposed.append(instance.entry_id)

        self._generations.refresh_references(self._plugin_registry.instances())
        return disposed

    async def _reclaim_after_drain(self) -> None:
        """Reclaim after the last lease of a draining generation was released.

        Shutdown owns reclamation while it runs: it holds the composition lock and
        is itself waiting for exactly this release, so taking the lock here would
        deadlock. Shutdown retires and disposes everything anyway.
        """

        if self._state in (HarnessState.STOPPING, HarnessState.STOPPED):
            return
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
            failures.extend(
                await self._observe(
                    HookEvent.PLUGIN_UNMOUNTING,
                    {
                        "plugin": instance.manifest.name,
                        "entry_id": instance.entry_id,
                        "instance_id": instance.instance_id,
                        "scope_id": instance.scope.id,
                    },
                )
            )
            try:
                await self._plugin_registry.dispose(instance)
            except EffectCleanupError as error:
                failures.extend(error.failures)
                for failure in error.failures:
                    self._emit_cleanup_failure(
                        failure,
                        entry_id=instance.entry_id,
                        scope=instance.scope.name,
                    )
                span.record_error(error)
            except BaseException as error:
                # Even a CancelledError raised inside disposal must not abort
                # the teardown loop: record it and move to the next instance.
                failure = CleanupFailure(
                    description=f"dispose of {instance.manifest.name!r}", error=error
                )
                failures.append(failure)
                self._emit_cleanup_failure(
                    failure, entry_id=instance.entry_id, scope=instance.scope.name
                )
                span.record_error(error)
            failures.extend(
                await self._observe(
                    HookEvent.PLUGIN_UNMOUNTED,
                    {
                        "plugin": instance.manifest.name,
                        "entry_id": instance.entry_id,
                        "instance_id": instance.instance_id,
                    },
                )
            )
            self._record_lifecycle(
                "plugin.unmount",
                {
                    "plugin": instance.manifest.name,
                    "entry_id": instance.entry_id,
                    "instance_id": instance.instance_id,
                },
            )

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
        self,
        generation: RuntimeGeneration,
        *,
        limits: BudgetLimits | None = None,
        scope: CompositionScope | str | None = None,
    ) -> RunEnvironment:
        """Assemble the generation-scoped services a run executes against.

        Everything is derived from ``generation``, so a run can never observe a
        service belonging to a different composition than its capabilities. When
        ``scope`` is given, the tool view is filtered to what that composition
        scope exposes, which is how an agent sees its own tools and not a
        sibling's.
        """

        effective_limits = limits if limits is not None else self._default_budget_limits
        policy: PolicyEngine
        try:
            policy = self._system_requirement(generation, POLICY, PolicyEngine, self._policy, scope)
        except ChassisError as error:
            policy = _FailClosedPolicy(error)
        secrets: SecretProvider
        try:
            # A provider registered for the secrets capability is handed out
            # through the shared redactor exactly like the default one: the
            # moment a value is read it can be scrubbed from anything emitted.
            secrets = redacting_secrets(
                self._system_requirement(generation, SECRETS, SecretProvider, self._secrets, scope),
                self._redactor,
            )
        except ChassisError as error:
            secrets = _FailClosedSecrets(error)
        return RunEnvironment(
            generation=generation,
            capabilities=self.scoped_capabilities(generation, scope),
            tools=self.tool_snapshot(generation, scope=scope),
            hooks=self.hook_snapshot(generation),
            executor=self._tool_executor,
            # A provider registered for these capabilities belongs to the
            # generation, so a run observes the policy and secret provider of the
            # composition it acquired rather than the harness default. A system
            # requirement that cannot be resolved installs a denying stand-in
            # instead of the default.
            policy=policy,
            secrets=secrets,
            telemetry=self._telemetry,
            redactor=self._redactor,
            budget=environment_budget(effective_limits),
        )

    def _system_requirement(
        self,
        generation: RuntimeGeneration,
        key: CapabilityKey,
        expected: Any,
        default: Any,
        scope: CompositionScope | str | None,
    ) -> Any:
        """Resolve one system requirement -- policy or secrets -- fail-closed.

        The configured default applies only when the generation registers no
        provider for ``key``. Registered providers are explicit system
        requirements: one eligible provider is selected, more than one is rejected
        unless an explicit preference selects one, and a registration that does
        not implement the required contract is a provider failure. Resolution
        failure therefore never widens back to the default.
        """

        snapshot = generation.snapshot
        all_registered = [item for item in snapshot.registrations if item.key.name == key.name]
        if not all_registered:
            return default
        resolved_scope = self._resolved_scope(generation, scope)
        if resolved_scope is None:
            registered = all_registered
        else:
            visible_provider_ids = resolved_scope.visible.get(key.name, ())
            registered = [
                item for item in all_registered if item.provider_id in visible_provider_ids
            ]
            # A system provider elsewhere in the composition is still an explicit
            # requirement. Treat its absence from this scope as a resolution
            # failure, rather than exposing the harness default (which may be
            # permissive or read process-wide secrets).
            if not registered:
                raise CapabilityVersionMismatch(
                    f"no provider of {key} is visible in this composition scope",
                    capability=str(key),
                    generation_id=generation.generation_id,
                    providers=[item.provider_id for item in all_registered],
                )
        eligible = [
            item for item in registered if item.key == key and isinstance(item.value, expected)
        ]
        if not eligible:
            raise CapabilityVersionMismatch(
                f"no provider of {key} implements the required contract",
                capability=str(key),
                generation_id=generation.generation_id,
                providers=[item.provider_id for item in registered],
            )
        if len(eligible) == 1:
            return eligible[0].value
        preferred = self._preferred_provider(key.name, scope)
        if preferred is not None:
            for item in eligible:
                if preferred in (item.registration_id, item.provider_id):
                    return item.value
            instance = self._plugin_registry.instance(preferred)
            if instance is not None:
                for item in eligible:
                    if item.provider_id == instance.instance_id:
                        return item.value
        raise CapabilityAmbiguous(
            f"capability {key} has several eligible providers and no explicit preference",
            capability=str(key),
            generation_id=generation.generation_id,
            providers=[item.provider_id for item in eligible],
        )

    def _preferred_provider(
        self, capability: str, scope: CompositionScope | str | None
    ) -> str | None:
        """Most specific explicit preference: scope first, then global."""

        effective = self._effective_preferences()
        if scope is not None:
            scoped = effective.get(f"scope:{self._scope_path(scope)}:{capability}")
            if scoped is not None:
                return scoped
        return effective.get(capability)

    def snapshot_for(
        self,
        generation: RuntimeGeneration,
        *,
        agent: str | None = None,
        agent_revision: str | None = None,
        agent_runtime: str | None = None,
        graph_definition_hash: str | None = None,
        prompt_hash: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RuntimeSnapshot:
        """Attributable metadata for one runtime generation.

        Configuration is redacted before it is hashed, so a snapshot explains the
        composition without carrying secret material. ``agent_runtime`` is the
        runtime identity reported by the selected execution engine; it is never
        assumed to be LangGraph.
        """

        return RuntimeSnapshot.from_generation(
            generation,
            tools=self.tool_snapshot(generation),
            redactor=self._redactor,
            agent=agent,
            agent_revision=agent_revision,
            agent_runtime=agent_runtime if agent_runtime is not None else "unknown",
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
                    generation,
                    agent=getattr(run_context, "agent", None) or None,
                    agent_revision=getattr(run_context, "agent_revision", None),
                )
        raise HarnessStateError(
            "run context refers to an unknown generation",
            generation_id=run_context.generation_id,
        )

    def scoped_capabilities(
        self, generation: RuntimeGeneration, scope: CompositionScope | str | None = None
    ) -> CapabilitySnapshot:
        """Capability snapshot filtered to what ``scope`` can see.

        Composition visibility, not authorization: the view selects the
        registrations a scope's lineage exposes, exactly like tool visibility.
        ``None`` returns the generation's full snapshot.
        """

        resolved = self._resolved_scope(generation, scope)
        if resolved is None:
            return generation.snapshot
        registrations = [
            registration
            for registration in generation.snapshot.registrations
            if registration.provider_id in resolved.visible.get(registration.key.name, ())
        ]
        return CapabilitySnapshot.from_registrations(generation.generation_id, registrations)

    def _resolved_scope(
        self, generation: RuntimeGeneration, scope: CompositionScope | str | None
    ) -> ResolvedScope | None:
        """The published scope named by ``scope``; unknown paths are rejected."""

        if scope is None:
            return None
        path = scope.path if isinstance(scope, CompositionScope) else scope
        resolved = generation.scopes.get(path)
        if resolved is None:
            raise ConfigurationError(
                "unknown composition scope for this generation",
                scope=path,
                generation_id=generation.generation_id,
            )
        return resolved

    def tool_snapshot(
        self,
        generation: RuntimeGeneration,
        *,
        scope: CompositionScope | str | None = None,
    ) -> ToolSnapshot:
        """Tools reachable from ``generation``, as an immutable view.

        A run executes against the tools of the generation it acquired: a plugin
        that left the composition stops contributing tools to new runs without
        disturbing runs already in flight. When ``scope`` is given, the view is
        narrowed to the tools that scope exposes, so sibling scopes never leak
        tools into each other. An unknown scope path is rejected rather than
        silently exposing an empty view.
        """

        snapshot = self._tool_registry.snapshot(
            generation.generation_id, owner_ids=generation.instance_ids
        )
        resolved = self._resolved_scope(generation, scope)
        if resolved is None:
            return snapshot
        visible = frozenset(resolved.visible_tools)
        return ToolSnapshot(
            generation.generation_id,
            (entry for entry in snapshot.entries if entry.name in visible),
        )

    def hook_snapshot(self, generation: RuntimeGeneration) -> HookSnapshot:
        """Hooks reachable from ``generation``, as an immutable view."""

        return self._hook_registry.snapshot(
            generation.generation_id, owner_ids=generation.instance_ids
        )

    # ------------------------------------------------------------- dry-run plan

    def plan(self, *, prefer: Mapping[str, str] | None = None) -> ResolutionPlan:
        """Resolve the desired state without applying it.

        The plan includes the composition scope tree and per-requirement
        provenance, so diagnostics can explain a resolution before it is
        published -- including why a requirement is still pending.
        """

        return self._resolver.resolve(
            self._plugin_registry.candidates(),
            prefer=self._effective_preferences() if prefer is None else prefer,
            scopes=self._composition.specs(),
        )

    def preview(self, config: Any = None) -> PlanResult:
        """Compute what the next apply would do, with zero mutation.

        Runs the full pipeline a subsequent ``apply_config`` + ``reconcile``
        would — parsing, migration, validation, catalog resolution,
        desired-state diffing, dependency resolution, semantic-identity reuse
        analysis, and generation-impact prediction — and returns the stable
        planning contract (:mod:`chassis.planning`). It mutates nothing: no
        revision bump, no dirty flag, no mounted plugin, no published
        generation, and no setup or cleanup effect. Plugin objects are
        constructed exactly as :meth:`install` would (so instance-configured
        manifests work) and discarded without mounting.

        Plan-level problems — unsatisfied or ambiguous requirements, cycles —
        are reported as ``reject`` actions rather than raised;
        configuration-level errors raise exactly as :meth:`apply_config` would.
        Publication-time contract validation needs mounted registrations and
        runs only during a real :meth:`reconcile`.

        Args:
            config: A declarative configuration to preview against the current
                state, or ``None`` to preview the next reconciliation of the
                current desired state.

        Raises:
            ConfigurationError: invalid or inconsistent configuration.
            PluginLoadError: a configured plugin cannot be constructed or does
                not declare a manifest.
        """

        entries: dict[str, PluginEntry] = {}
        changes: dict[str, DesiredStateChange] = {}
        preferences: dict[str, str]
        if config is None:
            entries = {entry.entry_id: entry for entry in self._plugin_registry.entries()}
            preferences = self._effective_preferences()
        else:
            document = parse_config(config)
            self._stage_config(document)
            config_preferences = dict(document.provider_preferences)
            for entry in document.enabled_entries:
                for capability, provider in entry.provider_preference.items():
                    config_preferences[f"{entry.id}:{capability}"] = provider
            preferences = {**config_preferences, **self._provider_preference}
            for change in diff_desired_state(document, self._installed_state()):
                changes[change.entry_id] = change
                if change.action is DesiredStateAction.REMOVE:
                    continue
                desired = document.entry(change.entry_id)
                if desired is not None:
                    entries[change.entry_id] = self._preview_entry(desired, change.action)

        plan = self._resolver.resolve(
            self._preview_candidates(entries, changes),
            prefer=preferences,
            scopes=self._composition.specs(),
        )
        return self._build_plan_result(plan, entries, changes, preferences)

    def _preview_entry(self, desired: PluginEntryConfig, action: DesiredStateAction) -> PluginEntry:
        """The entry an apply would install — constructed, never installed.

        The revision mirrors what ``install`` would assign: a replacement bumps
        it (forcing a rebuild), an unchanged entry keeps it (so reuse is
        predicted), and a fresh entry starts at 1.
        """

        plugin_type = self._catalog.get(desired.plugin)
        plugin_object = (
            plugin_type(desired.config) if isinstance(plugin_type, type) else plugin_type
        )
        manifest = getattr(plugin_object, "manifest", None)
        if not isinstance(manifest, PluginManifest):
            raise PluginLoadError("plugin does not declare a PluginManifest", plugin=desired.plugin)
        existing = self._plugin_registry.entry(desired.id)
        if existing is None or action is DesiredStateAction.ADD:
            revision = 1
        elif action is DesiredStateAction.REPLACE:
            revision = existing.revision + 1
        else:
            revision = existing.revision
        return PluginEntry(
            entry_id=desired.id,
            plugin=plugin_object,
            manifest=manifest,
            config=freeze(desired.config),
            revision=revision,
        )

    def _preview_candidates(
        self, entries: Mapping[str, PluginEntry], changes: Mapping[str, DesiredStateChange]
    ) -> tuple[PluginCandidate, ...]:
        """Resolver input for the previewed state, mirroring a real reconcile.

        A replaced entry resolves as declaring-only, exactly like a real apply:
        its revision is bumped before resolution, so the outgoing instance's
        registrations no longer count as its active provisions.
        """

        existing = {
            candidate.entry_id: candidate for candidate in self._plugin_registry.candidates()
        }
        candidates: list[PluginCandidate] = []
        for entry_id in sorted(entries):
            entry = entries[entry_id]
            change = changes.get(entry_id)
            mutating = change is not None and change.action in (
                DesiredStateAction.ADD,
                DesiredStateAction.REMOVE,
                DesiredStateAction.REPLACE,
            )
            current = existing.get(entry_id)
            if current is None or mutating:
                candidates.append(
                    PluginCandidate(
                        entry_id=entry_id,
                        manifest=entry.manifest,
                        instance_id=None if current is None else current.instance_id,
                        active=False,
                        registrations=(),
                        scope=entry.scope,
                    )
                )
            else:
                candidates.append(current)
        return tuple(candidates)

    def _build_plan_result(
        self,
        plan: ResolutionPlan,
        entries: Mapping[str, PluginEntry],
        changes: Mapping[str, DesiredStateChange],
        preferences: Mapping[str, str],
    ) -> PlanResult:
        """Walk the plan exactly like materialization would, without mounting."""

        identities: dict[str, SemanticIdentity] = {}
        reused: dict[str, bool] = {}
        actions: dict[str, PlanAction] = {}

        def predicted_instance(provider_entry: str) -> str | None:
            if reused.get(provider_entry) is False:
                # The provider will be a new instance; binding to the outgoing
                # one would predict false reuse for its consumers.
                return f"pending:{provider_entry}"
            instance = self._plugin_registry.instance(provider_entry)
            return None if instance is None else instance.instance_id

        for entry_id in plan.activation_order:
            entry = entries.get(entry_id)
            if entry is None:  # pragma: no cover - defensive
                continue
            change = changes.get(entry_id)
            identity = self._semantic_identity(
                entry,
                plan,
                identities,
                preferences=preferences,
                provider_instance=predicted_instance,
            )
            identities[entry_id] = identity
            existing = self._plugin_registry.instance(entry_id)
            untouched = change is None or change.action is DesiredStateAction.UNCHANGED
            will_reuse = (
                untouched
                and existing is not None
                and existing.state is PluginState.ACTIVE
                and existing.entry_revision == entry.revision
                and existing.semantic_identity == identity
            )
            reused[entry_id] = will_reuse
            actions[entry_id] = self._plan_action(entry, change, identity, existing, will_reuse)

        for entry_id, change in changes.items():
            if change.action is DesiredStateAction.REMOVE:
                actions[entry_id] = self._remove_action(entry_id, change)

        for planned in plan.plugins:
            if planned.entry_id in actions:
                continue
            actions[planned.entry_id] = self._reject_action(
                planned,
                entries.get(planned.entry_id),
                plan,
                preferences,
            )

        entry_actions = tuple(actions[entry_id] for entry_id in sorted(actions))
        forcing = any(
            action.generation_impact is GenerationImpact.NEW_GENERATION for action in entry_actions
        )
        current = self._generations.current
        would_publish = True
        if not forcing and current is not None:
            # Everything is reused: the exact no-op test a reconcile performs.
            instances = tuple(
                instance
                for entry_id in plan.activation_order
                if (instance := self._plugin_registry.instance(entry_id)) is not None
            )
            scope_tree = build_scope_tree(
                plan=plan,
                instances=instances,
                registrations=self._capability_registry.registrations(),
                tools=self._tool_registry.entries(),
            )
            would_publish = not self._same_composition(
                current, instances, self._snapshot_factory(instances), scope_tree
            )

        publication = PlanAction(
            action=ActionKind.PUBLISH if would_publish else ActionKind.NOOP,
            reasons=(
                (
                    ReasonCode.COMPOSITION_CHANGED
                    if would_publish
                    else ReasonCode.COMPOSITION_UNCHANGED
                ),
            ),
            generation_impact=(
                GenerationImpact.NEW_GENERATION if would_publish else GenerationImpact.NONE
            ),
        )
        return PlanResult(
            chassis_version=chassis_version(),
            actions=(*entry_actions, publication),
            would_publish=would_publish,
            current_generation_id=None if current is None else current.generation_id,
            pending=tuple(plan.pending),
            cycles=tuple(tuple(cycle) for cycle in plan.cycles),
            preferences=dict(preferences),
        )

    def _plan_action(
        self,
        entry: PluginEntry,
        change: DesiredStateChange | None,
        identity: SemanticIdentity,
        existing: PluginInstance | None,
        will_reuse: bool,
    ) -> PlanAction:
        """Classify one eligible entry exactly like materialization would."""

        common: dict[str, Any] = {
            "entry_id": entry.entry_id,
            "plugin": entry.manifest.identity,
            "scope": entry.scope,
            "config_keys": tuple(sorted(entry.config)),
        }
        if change is not None and change.action is DesiredStateAction.ADD:
            return PlanAction(
                action=ActionKind.ADD,
                reasons=(ReasonCode.ENTRY_ADDED,),
                generation_impact=GenerationImpact.NEW_GENERATION,
                **common,
            )
        if change is not None and change.action is DesiredStateAction.REPLACE:
            old = self._plugin_registry.entry(entry.entry_id)
            old_fingerprint = (
                None
                if old is None
                else config_fingerprint(plugin=old.manifest.name, config=old.config)
            )
            configured = old_fingerprint != config_fingerprint(
                plugin=entry.manifest.name, config=entry.config
            )
            return PlanAction(
                action=ActionKind.REPLACE,
                reasons=(
                    (
                        ReasonCode.CONFIGURATION_CHANGED
                        if configured
                        else ReasonCode.IMPLEMENTATION_CHANGED
                    ),
                ),
                generation_impact=GenerationImpact.NEW_GENERATION,
                **common,
            )
        if existing is None:
            # Never mounted: whatever its desired history, the composition gains
            # it fresh.
            return PlanAction(
                action=ActionKind.ADD,
                reasons=(ReasonCode.ENTRY_ADDED,),
                generation_impact=GenerationImpact.NEW_GENERATION,
                **common,
            )
        if will_reuse:
            return PlanAction(
                action=ActionKind.REUSE,
                reasons=(ReasonCode.SEMANTIC_IDENTITY_UNCHANGED,),
                expected_reuse=True,
                instance_id=existing.instance_id,
                **common,
            )

        reasons: list[ReasonCode] = []
        cause_capability: str | None = None
        cause_provider: str | None = None
        if existing.entry_revision != entry.revision:
            reasons.append(ReasonCode.REVISION_CHANGED)
        previous = existing.semantic_identity
        if previous is not None:
            reasons.extend(
                sorted(
                    {
                        INPUT_REASONS[name]
                        for name in previous.changed_inputs(identity)
                        if name in INPUT_REASONS
                    },
                    key=lambda reason: reason.value,
                )
            )
            old_bindings = {binding.capability: binding for binding in previous.bindings}
            for binding in identity.bindings:
                old_binding = old_bindings.get(binding.capability)
                if old_binding is None or old_binding.semantic_key() != binding.semantic_key():
                    cause_capability = binding.capability
                    cause_provider = binding.provider_entry_id
                    break
        if existing.state is not PluginState.ACTIVE:
            reasons.append(ReasonCode.INSTANCE_INACTIVE)
        return PlanAction(
            action=ActionKind.REBUILD,
            reasons=tuple(reasons),
            cause_capability=cause_capability,
            cause_provider=cause_provider,
            generation_impact=GenerationImpact.NEW_GENERATION,
            **common,
        )

    def _remove_action(self, entry_id: str, change: DesiredStateChange) -> PlanAction:
        """An entry installed but no longer desired."""

        entry = self._plugin_registry.entry(entry_id)
        existing = self._plugin_registry.instance(entry_id)
        active = existing is not None and existing.state is PluginState.ACTIVE
        return PlanAction(
            action=ActionKind.REMOVE,
            reasons=(ReasonCode.ENTRY_REMOVED,),
            entry_id=entry_id,
            plugin=change.plugin if change.plugin is not None else change.entry_id,
            scope="/" if entry is None else entry.scope,
            instance_id=None if existing is None else existing.instance_id,
            generation_impact=(
                GenerationImpact.NEW_GENERATION if active else GenerationImpact.NONE
            ),
        )

    def _reject_action(
        self,
        planned: PluginPlan,
        entry: PluginEntry | None,
        plan: ResolutionPlan,
        preferences: Mapping[str, str],
    ) -> PlanAction:
        """An entry that cannot join the composition, with structured reasons."""

        reasons: list[ReasonCode] = []
        failures: list[ValidationFailure] = []
        ambiguities: list[Ambiguity] = []
        scope = planned.scope
        consumer = planned.entry_id
        for resolution in planned.requirements:
            if resolution.status == "resolved":
                continue
            requirement = str(resolution.requirement)
            if resolution.status == "ambiguous":
                reasons.append(ReasonCode.REQUIREMENT_AMBIGUOUS)
                candidates = tuple(
                    dict.fromkeys(
                        assessment.provider_entry_id
                        for assessment in resolution.assessments
                        if assessment.eligible and assessment.visible
                    )
                )
                ambiguities.append(
                    Ambiguity(
                        consumer=consumer,
                        capability=resolution.requirement.name,
                        requirement=requirement,
                        candidates=candidates,
                        preference=preference_for(
                            preferences, consumer, resolution.requirement.name, scope
                        ),
                    )
                )
                continue
            if resolution.status == "provider_pending":
                reasons.append(ReasonCode.PENDING_DEPENDENCY)
            else:
                reasons.append(ReasonCode.REQUIREMENT_UNSATISFIED)
            failures.append(ValidationFailure(code=resolution.status, detail=requirement))

        for cycle in plan.cycles:
            if planned.entry_id in cycle:
                reasons.append(ReasonCode.PLUGIN_CYCLE)
                failures.append(
                    ValidationFailure(code="plugin_cycle", detail=" -> ".join((*cycle, cycle[0])))
                )

        existing = self._plugin_registry.instance(planned.entry_id)
        active = existing is not None and existing.state is PluginState.ACTIVE
        return PlanAction(
            action=ActionKind.REJECT,
            reasons=tuple(dict.fromkeys(reasons)),
            entry_id=planned.entry_id,
            plugin=None if entry is None else entry.manifest.identity,
            scope=scope,
            config_keys=() if entry is None else tuple(sorted(entry.config)),
            validation_failures=tuple(failures),
            ambiguities=tuple(ambiguities),
            generation_impact=(
                GenerationImpact.NEW_GENERATION if active else GenerationImpact.NONE
            ),
        )

    def entry(self, entry_id: str) -> PluginEntry | None:
        return self._plugin_registry.entry(entry_id)

    def entry_restorer(self, entry_id: str) -> Callable[[], Any]:
        """Undo action restoring one desired entry exactly as captured now.

        Transactional batches (``apply_config``, AgentSpec materialization)
        register these before mutating an entry. Restoring the captured state
        directly keeps a rollback from re-running installation: no revision
        bump, no new entry object, no resurrection of an entry that was never
        removed.
        """

        captured = self._plugin_registry.capture_entry(entry_id)

        def restore() -> None:
            self._plugin_registry.restore_entry(entry_id, captured)

        return restore

    def restore_pending_changes(self, pending: bool) -> None:
        """Restore the pending-changes flag captured before a failed batch."""

        self._dirty = pending

    # -------------------------------------------------------- context manager

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc_details: object) -> bool:
        await self.stop()
        return False
