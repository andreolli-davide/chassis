"""Agent registration and invocation.

The registry is backend-agnostic: it stores :class:`~chassis.runtime.AgentRuntime`
implementations and, for each invocation, acquires the current runtime generation,
assembles the generation's environment, and hands an immutable run context to the
execution engine.

A run therefore observes exactly the composition it acquired. Invocation never
takes the control-plane lock: acquisition is a lease on an immutable generation,
and execution happens without it (invariant I8).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from chassis.agent_spec import AgentRevision, AgentSpec, composition_payload
from chassis.budget.governor import BudgetGovernor, budget_scope, current_budget
from chassis.budget.models import BudgetLimits
from chassis.core.errors import (
    AgentExecutionError,
    ChassisError,
    ConfigurationError,
    HarnessStateError,
    PolicyDenied,
)
from chassis.core.scope import Scope
from chassis.hooks.registry import HookSnapshot
from chassis.hooks.types import HookEvent
from chassis.persistence.hashing import stable_hash
from chassis.runtime import (
    AgentEvent,
    AgentRequest,
    AgentResult,
    AgentRuntime,
    HarnessRunContext,
    RunEnvironment,
)
from chassis.secrets.redaction import redact_config

if TYPE_CHECKING:
    from chassis.core.generation import RuntimeGeneration
    from chassis.harness import Harness

__all__ = [
    "AgentNotFound",
    "AgentRegistry",
    "AgentRetired",
    "AgentRevision",
    "AgentSpec",
    "ScopedAgents",
]

#: Prefix of the plugin entry ids an agent's scope declares.
_AGENT_ENTRY_PREFIX = "agent:"
#: Reserved scope-metadata keys identifying the agent revision a scope materialized.
_AGENT_METADATA_KEY = "chassis.agent"
_AGENT_REVISION_METADATA_KEY = "chassis.agent_revision"


class AgentNotFound(ChassisError):
    """Raised when an agent name is not registered."""

    code = "agent_not_found"


class AgentRetired(ChassisError):
    """Raised when a new run selects an agent that has been retired.

    Retirement means *no new execution selects it*; runs already pinned to a
    published revision keep observing it, and its resources are disposed only when
    no live generation reaches them.
    """

    code = "agent_retired"


@dataclass(frozen=True, slots=True)
class _RuntimeRegistration:
    """One identity-keyed agent-runtime registration."""

    registration_id: str
    name: str
    runtime: AgentRuntime
    scope_id: str | None = None


class AgentRegistry:
    """Registered execution engines, plus the app-facing invocation surface.

    Args:
        harness: Owning harness, used to acquire generations and assemble the run
            environment. ``None`` disables invocation (registration only), which is
            useful for isolated unit tests of an agent runtime.
    """

    def __init__(self, *, harness: Harness | None = None) -> None:
        self._harness = harness
        self._entries: dict[str, _RuntimeRegistration] = {}
        self._names: dict[str, list[str]] = {}
        self._spec_history: dict[str, dict[str, AgentRevision]] = {}
        self._active_revisions: dict[str, str] = {}
        self._retired: set[str] = set()
        self._owned_scope_paths: set[str] = set()

    # ------------------------------------------------------------- registration

    def register(
        self,
        runtime: AgentRuntime,
        *,
        scope: Scope | None = None,
        replace: bool = False,
    ) -> AgentRuntime:
        """Register an agent runtime under its name.

        Registrations are identity-keyed and owned by ``scope`` when one is
        given, so unloading the plugin that provided the agent removes exactly
        that registration — never a successor's — and an old run keeps the
        runtime object it already resolved.
        """

        name = getattr(runtime, "name", "")
        if not name:
            raise ConfigurationError(
                "agent runtime must declare a name", runtime=type(runtime).__name__
            )
        if scope is not None:
            scope.assert_open(f"register agent {name!r}")
        if self._names.get(name) and not replace:
            raise ConfigurationError("agent name is already registered", agent=name)
        registration = _RuntimeRegistration(
            registration_id=f"agent_{uuid.uuid4().hex[:12]}",
            name=name,
            runtime=runtime,
            scope_id=None if scope is None else scope.id,
        )
        self._entries[registration.registration_id] = registration
        self._names.setdefault(name, []).append(registration.registration_id)
        if scope is not None:
            scope.cleanup(
                f"agent {name}", self._release, registration.registration_id, kind="agent"
            )
        return runtime

    def _release(self, registration_id: str) -> bool:
        """Remove one registration by identity; never a same-named successor."""

        registration = self._entries.pop(registration_id, None)
        if registration is None:
            return False
        ids = self._names.get(registration.name, [])
        if registration_id in ids:
            ids.remove(registration_id)
        if not ids:
            self._names.pop(registration.name, None)
        return True

    def unregister(self, name: str, *, scope_id: str | None = None) -> bool:
        """Remove registrations of ``name`` early.

        With ``scope_id`` only that scope's registrations are removed; without
        it, the newest registration is removed.
        """

        ids = list(self._names.get(name, []))
        if scope_id is None:
            return self._release(ids[-1]) if ids else False
        removed = False
        for registration_id in ids:
            if self._entries[registration_id].scope_id == scope_id:
                removed = self._release(registration_id) or removed
        return removed

    def get(self, name: str) -> AgentRuntime:
        """Return the newest registered runtime of ``name``.

        Raises:
            AgentNotFound: the name is not registered.
        """

        ids = self._names.get(name)
        if not ids:
            raise AgentNotFound(
                f"agent {name!r} is not registered", agent=name, available=list(self.names())
            )
        return self._entries[ids[-1]].runtime

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._names))

    def __len__(self) -> int:
        return len(self._names)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._names

    def to_dict(self) -> dict[str, Any]:
        return {
            "agents": list(self.names()),
            "specs": [revision.to_dict() for revision in self.specs()],
            "revisions": {name: list(self.revisions(name)) for name in sorted(self._spec_history)},
            "retired": sorted(self._retired),
        }

    # ------------------------------------------------------------ agent specs

    def install(self, spec: AgentSpec, *, replace: bool = False) -> AgentRevision:
        """Publish an immutable agent revision and materialize its composition.

        The spec materializes through the existing composition primitives: a
        :class:`~chassis.composition.CompositionScope` at ``spec.scope_path`` owns
        the plugin contributions, the capability and tool views, the scope
        requirements, and the agent's reserved metadata. Nothing here introduces a
        second composition engine.

        Args:
            spec: The composition description to publish.
            replace: Required to make a different revision current for a name that
                already has one. A ``(name, revision)`` pair that was published with
                different content is rejected: revisions are immutable.

        Raises:
            ConfigurationError: the revision is already published with different
                content, a different revision is active without ``replace``, or a
                declared plugin reference is unknown.
        """

        self._require_harness()
        history = self._spec_history.setdefault(spec.name, {})
        published = history.get(spec.revision)
        if published is not None and published.spec != spec:
            raise ConfigurationError(
                "a published agent revision is immutable; declare a new revision",
                agent=spec.name,
                revision=spec.revision,
            )
        active = self._active_revisions.get(spec.name)
        if active == spec.revision and published is not None:
            return published
        if active is not None and active != spec.revision and not replace:
            raise ConfigurationError(
                "agent already has an active revision; pass replace=True to change it",
                agent=spec.name,
                active=active,
                requested=spec.revision,
            )
        previous: AgentRevision | None = None
        if active is not None and active != spec.revision:
            candidate = history.get(active)
            if candidate is not None and candidate.scope != spec.scope_path:
                # The revision moves to a different scope: withdraw the old one
                # entirely. A revision that stays in the same scope is reconciled
                # in place so unchanged contributions stay mounted and reusable.
                self._withdraw(candidate)
            else:
                previous = candidate
        revision = self._materialize(spec, previous=previous)
        history[spec.revision] = revision
        self._active_revisions[spec.name] = spec.revision
        self._retired.discard(spec.name)
        return revision

    def replace(self, spec: AgentSpec) -> AgentRevision:
        """Publish ``spec`` as the current revision of its agent."""

        return self.install(spec, replace=True)

    def remove(self, name: str) -> bool:
        """Retire an agent: no new run selects it.

        The active revision's contributions leave desired state, so the next
        generation no longer contains them. Published generations, and the
        resources they reach, are unaffected: a run pinned to an older generation
        keeps observing it until its lease ends. Historical revisions stay
        reachable for diagnostics and diffing.
        """

        history = self._spec_history.get(name)
        if history is None:
            return False
        self._retired.add(name)
        active = self._active_revisions.pop(name, None)
        if active is not None:
            previous = history.get(active)
            if previous is not None:
                self._withdraw(previous)
        return True

    def is_retired(self, name: str) -> bool:
        """Whether an agent has been retired and no new run selects it."""

        return name in self._retired

    def active_spec(self, name: str) -> AgentRevision | None:
        """The revision new runs select, or ``None`` when none is active."""

        revision = self._active_revisions.get(name)
        if revision is None:
            return None
        return self._spec_history.get(name, {}).get(revision)

    def spec(self, name: str, revision: str | None = None) -> AgentRevision:
        """Return a published revision, active when ``revision`` is omitted.

        Raises:
            AgentNotFound: the agent or revision is unknown.
            AgentRetired: the agent has been retired and no revision was named.
        """

        history = self._spec_history.get(name)
        if history is None:
            raise AgentNotFound(
                f"agent {name!r} has no published spec",
                agent=name,
                available=sorted(self._spec_history),
            )
        if revision is None:
            revision = self._active_revisions.get(name)
            if revision is None:
                raise AgentRetired(
                    "agent has been retired; name a revision to inspect it",
                    agent=name,
                    revisions=sorted(history),
                )
        published = history.get(revision)
        if published is None:
            raise AgentNotFound(
                f"agent {name!r} has no revision {revision!r}",
                agent=name,
                revision=revision,
                revisions=sorted(history),
            )
        return published

    def revisions(self, name: str) -> tuple[str, ...]:
        """Published revisions of one agent, in the order they were published."""

        return tuple(self._spec_history.get(name, {}))

    def specs(self) -> tuple[AgentRevision, ...]:
        """The active revision of every agent, in deterministic order."""

        active = [
            revision
            for name in sorted(self._active_revisions)
            if (revision := self.active_spec(name)) is not None
        ]
        return tuple(active)

    def history(self) -> tuple[AgentRevision, ...]:
        """Every published revision, newest publication last, name-major order."""

        collected: list[AgentRevision] = []
        for name in sorted(self._spec_history):
            collected.extend(self._spec_history[name].values())
        return tuple(collected)

    def _entry_id(self, agent: str, plugin: str) -> str:
        return f"{_AGENT_ENTRY_PREFIX}{agent}:{plugin}"

    def _materialize(
        self, spec: AgentSpec, *, previous: AgentRevision | None = None
    ) -> AgentRevision:
        """Declare the spec's composition in the control plane.

        Reuses the exact machinery a hand-authored scope uses: the scope owns its
        entries, its capability and tool views narrow resolution and visibility,
        and its requirements are resolved with provenance. Desired-state changes
        take effect on the next reconciliation.
        """

        harness = self._require_harness()
        tree = harness.composition
        path = spec.scope_path
        existed = tree.get(path) is not None
        scope = tree.ensure(path)
        if not existed:
            self._owned_scope_paths.add(path)

        if spec.capabilities is None:
            scope.unrestrict()
        else:
            scope.restrict(*sorted(spec.capabilities))
        if spec.tools is None:
            scope.expose_all_tools()
        else:
            scope.select_tools(*sorted(spec.tools))

        for requirement in scope.requirements:
            scope.drop_requirement(requirement.name)
        for name, specifier in sorted(spec.requires.items()):
            scope.require(name, specifier)
        for name, specifier in sorted(spec.optional.items()):
            scope.require(name, specifier, optional=True)

        # An agent scope is agent-owned, so the reserved keys are safe. They are
        # what lets a published generation say which revision it materialized,
        # which is how a run stays pinned to the revision it started with.
        metadata = dict(spec.metadata)
        metadata[_AGENT_METADATA_KEY] = spec.name
        metadata[_AGENT_REVISION_METADATA_KEY] = spec.revision
        scope.set_metadata(metadata)

        entries = self._install_contributions(spec, path)
        if previous is not None:
            for entry_id in previous.entries:
                if entry_id not in entries:
                    harness.uninstall(entry_id)
        digest = stable_hash(redact_config(composition_payload(spec), harness.redactor))
        return AgentRevision(spec=spec, scope=path, entries=entries, composition_digest=digest)

    def _install_contributions(self, spec: AgentSpec, path: str) -> tuple[str, ...]:
        """Declare each plugin contribution as an entry local to the agent scope.

        An unchanged contribution is left exactly as it is, so its entry revision
        does not move and 0.4 incremental reuse can carry the mounted instance
        across the revision change.
        """

        harness = self._require_harness()
        entries: list[str] = []
        for plugin_name, value in sorted(spec.plugins.items()):
            entry_id = self._entry_id(spec.name, plugin_name)
            existing = harness.entry(entry_id)
            if isinstance(value, Mapping):
                config = dict(value)
                plugin_type = harness.catalog.get(plugin_name)
                if (
                    existing is not None
                    and existing.manifest.name == plugin_name
                    and dict(existing.config) == config
                    and existing.scope == path
                ):
                    entries.append(entry_id)
                    continue
                harness.install(
                    plugin_type,
                    entry_id=entry_id,
                    config=config,
                    replace=existing is not None,
                    scope=path,
                )
            else:
                harness.install(
                    value,
                    entry_id=entry_id,
                    replace=existing is not None,
                    scope=path,
                )
            entries.append(entry_id)
        return tuple(sorted(entries))

    def _withdraw(self, revision: AgentRevision) -> None:
        """Withdraw a revision's contributions from desired state."""

        harness = self._require_harness()
        tree = harness.composition
        if revision.scope in self._owned_scope_paths and tree.get(revision.scope) is not None:
            tree.remove(revision.scope)
            self._owned_scope_paths.discard(revision.scope)
            return
        for entry_id in revision.entries:
            harness.uninstall(entry_id)

    # ----------------------------------------------------------- run selection

    def _resolve_agent(self, agent: str) -> tuple[AgentRuntime, AgentRevision | None]:
        """Resolve the runtime and the revision new runs select for ``agent``.

        An agent published through a spec selects its active revision and the
        runtime that revision references. An agent that was only registered as a
        runtime keeps working unchanged, with no revision attribution.
        """

        revision: AgentRevision | None = None
        if agent in self._spec_history:
            revision = self.active_spec(agent)
            if revision is None:
                raise AgentRetired(
                    "agent has been retired; no new run selects it",
                    agent=agent,
                    revisions=sorted(self._spec_history[agent]),
                )
        runtime_name = agent
        if revision is not None and revision.runtime_ref is not None:
            runtime_name = revision.runtime_ref
        return self.get(runtime_name), revision

    def _pin_revision(
        self,
        agent: str,
        selected: AgentRevision | None,
        generation: RuntimeGeneration,
    ) -> AgentRevision | None:
        """The revision the acquired generation actually materialized.

        The active revision and the acquired generation normally agree, because
        invocation reconciles pending changes first. If a concurrent publication
        slipped in between, the generation wins: a run is attributed to the
        composition it acquired, never to one that replaced it.
        """

        if selected is None:
            return None
        resolved = generation.scopes.get(selected.scope)
        if resolved is None:
            return selected
        revision = resolved.metadata.get(_AGENT_REVISION_METADATA_KEY)
        if not isinstance(revision, str) or revision == selected.revision:
            return selected
        published = self._spec_history.get(agent, {}).get(revision)
        return selected if published is None else published

    # -------------------------------------------------------------- invocation

    async def invoke(
        self,
        agent: str,
        input: Any = None,
        *,
        request: AgentRequest | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
        thread_id: str | None = None,
        resume: Any = None,
        checkpoint_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        limits: BudgetLimits | None = None,
    ) -> AgentResult:
        """Execute ``agent`` once and return its result.

        Either ``input`` or a fully formed ``request`` may be supplied, not both.
        """

        harness = self._require_harness()
        runtime, selected = self._resolve_agent(agent)
        agent_request = self._build_request(
            request,
            input=input,
            thread_id=thread_id,
            resume=resume,
            checkpoint_id=checkpoint_id,
            metadata=metadata,
        )

        await harness.ensure_ready()
        async with harness.acquire() as generation:
            pinned = self._pin_revision(agent, selected, generation)
            agent_revision = None if pinned is None else pinned.revision
            environment = self._budgeted_environment(
                harness,
                generation,
                limits,
                scope=None if pinned is None else pinned.scope,
            )
            run_context = HarnessRunContext.new(
                generation=generation,
                environment=environment,
                agent=agent,
                agent_revision=agent_revision,
                user_id=user_id,
                tenant_id=tenant_id,
                thread_id=agent_request.thread_id,
                metadata=agent_request.metadata,
            )
            graph_digest = _definition_digest(runtime, run_context)
            snapshot = harness.snapshot_for(
                generation,
                agent=agent,
                agent_revision=agent_revision,
                graph_definition_hash=graph_digest,
            )
            hooks = harness.hook_snapshot(generation)
            run_payload: dict[str, Any] = {
                "agent": agent,
                "agent_revision": agent_revision,
                "generation_id": generation.generation_id,
                "thread_id": agent_request.thread_id,
                "user_id": user_id,
                "tenant_id": tenant_id,
            }
            started = time.monotonic()
            with _budget_scope(environment):
                async with harness.telemetry.span(
                    "agent.run",
                    {
                        "agent": agent,
                        "agent_revision": agent_revision,
                        "generation_id": generation.generation_id,
                        "thread_id": agent_request.thread_id,
                        "chassis_version": snapshot.chassis_version,
                        "snapshot_digest": snapshot.digest(),
                        "plugin_graph_hash": snapshot.plugin_graph_hash,
                        "graph_definition_hash": graph_digest,
                        "tool_schema_hash": snapshot.tool_schema_hash,
                    },
                ):
                    await self._dispatch_agent(
                        harness, HookEvent.BEFORE_AGENT_RUN, run_payload, hooks, refusable=True
                    )
                    try:
                        result = await runtime.invoke(agent_request, run_context)
                    except Exception as error:
                        await self._dispatch_agent(
                            harness,
                            HookEvent.AGENT_ERROR,
                            {
                                **run_payload,
                                "error": harness.redactor.redact_and_report(str(error))[0],
                                "error_type": type(error).__name__,
                            },
                            hooks,
                        )
                        if isinstance(error, ChassisError):
                            raise
                        message, _ = harness.redactor.redact_and_report(
                            f"agent {agent!r} failed: {error}"
                        )
                        raise AgentExecutionError(
                            message, agent=agent, error_type=type(error).__name__
                        ) from error
                    if result.interrupted:
                        _record_interrupts(harness, agent, agent_request.thread_id, result)
                    await self._dispatch_agent(
                        harness,
                        HookEvent.AFTER_AGENT_RUN,
                        {
                            **run_payload,
                            "status": "interrupted" if result.interrupted else "ok",
                            "duration_seconds": result.duration_seconds,
                        },
                        hooks,
                    )
            return _with_duration(
                _with_attribution(result, agent, agent_revision, snapshot.digest()),
                time.monotonic() - started,
            )

    async def stream(
        self,
        agent: str,
        input: Any = None,
        *,
        request: AgentRequest | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
        thread_id: str | None = None,
        resume: Any = None,
        checkpoint_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        limits: BudgetLimits | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Execute ``agent`` and yield events as they occur.

        The generation is leased for as long as the stream is consumed.
        """

        harness = self._require_harness()
        runtime, selected = self._resolve_agent(agent)
        agent_request = self._build_request(
            request,
            input=input,
            thread_id=thread_id,
            resume=resume,
            checkpoint_id=checkpoint_id,
            metadata=metadata,
        )

        await harness.ensure_ready()
        async with harness.acquire() as generation:
            pinned = self._pin_revision(agent, selected, generation)
            agent_revision = None if pinned is None else pinned.revision
            environment = self._budgeted_environment(
                harness,
                generation,
                limits,
                scope=None if pinned is None else pinned.scope,
            )
            run_context = HarnessRunContext.new(
                generation=generation,
                environment=environment,
                agent=agent,
                agent_revision=agent_revision,
                user_id=user_id,
                tenant_id=tenant_id,
                thread_id=agent_request.thread_id,
                metadata=agent_request.metadata,
            )
            hooks = harness.hook_snapshot(generation)
            run_payload: dict[str, Any] = {
                "agent": agent,
                "agent_revision": agent_revision,
                "generation_id": generation.generation_id,
                "thread_id": agent_request.thread_id,
                "user_id": user_id,
                "tenant_id": tenant_id,
            }
            await self._dispatch_agent(
                harness, HookEvent.BEFORE_AGENT_RUN, run_payload, hooks, refusable=True
            )
            try:
                with _budget_scope(environment):
                    async for event in runtime.stream(agent_request, run_context):
                        yield event
            except Exception as error:
                await self._dispatch_agent(
                    harness,
                    HookEvent.AGENT_ERROR,
                    {
                        **run_payload,
                        "error": harness.redactor.redact_and_report(str(error))[0],
                        "error_type": type(error).__name__,
                    },
                    hooks,
                )
                if isinstance(error, ChassisError):
                    raise
                message, _ = harness.redactor.redact_and_report(f"agent {agent!r} failed: {error}")
                raise AgentExecutionError(
                    message, agent=agent, error_type=type(error).__name__
                ) from error
            await self._dispatch_agent(
                harness, HookEvent.AFTER_AGENT_RUN, {**run_payload, "status": "ok"}, hooks
            )

    # -------------------------------------------------------------- internals

    def _budgeted_environment(
        self,
        harness: Harness,
        generation: RuntimeGeneration,
        limits: BudgetLimits | None,
        *,
        scope: str | None = None,
    ) -> RunEnvironment:
        """Assemble the run environment, inheriting a parent run's budget when nested.

        A run started from inside another run takes a child allocation of the parent's
        budget: the child's consumption propagates upward, so a parent can never be
        overdrawn by its children, and the nested run counts against
        :attr:`~chassis.budget.BudgetDimension.CHILD_RUNS`, which is enforced here --
        a boundary the harness mediates. ``scope`` narrows the tool view to the
        agent's composition scope when it was published through an ``AgentSpec``.
        """

        environment = harness.run_environment(generation, limits=limits, scope=scope)
        parent = current_budget()
        if parent is None:
            return environment
        return replace(environment, budget=parent.child(limits))

    async def _dispatch_agent(
        self,
        harness: Harness,
        event: HookEvent,
        payload: Mapping[str, Any],
        hooks: HookSnapshot,
        *,
        refusable: bool = False,
    ) -> None:
        """Dispatch a hook at the agent-run boundary.

        Only ``before_agent_run`` is refusable: a handler that bails there stops the
        run before the engine is invoked, reported as ``PolicyDenied`` exactly as a
        tool refusal by a hook is. Later boundaries are notifications, so a bail is
        reported to the remaining handlers but not acted on.
        """

        result = await harness.hooks.dispatch(event, payload, hooks=hooks)
        if refusable and result.stopped:
            raise PolicyDenied(
                f"agent {payload['agent']!r} was refused by a hook",
                agent=payload["agent"],
                reason="hook",
            )

    def _require_harness(self) -> Harness:
        if self._harness is None:
            raise HarnessStateError("agent registry is not bound to a harness")
        return self._harness

    @staticmethod
    def _build_request(
        request: AgentRequest | None,
        *,
        input: Any,
        thread_id: str | None,
        resume: Any,
        checkpoint_id: str | None,
        metadata: Mapping[str, Any] | None,
    ) -> AgentRequest:
        if request is not None:
            if input is not None:
                raise ConfigurationError("pass either input or request, not both")
            return request
        return AgentRequest(
            input=input,
            thread_id=thread_id,
            resume=resume,
            checkpoint_id=checkpoint_id,
            metadata=dict(metadata or {}),
        )


class ScopedAgents:
    """Plugin-facing facade bound to one plugin instance scope."""

    __slots__ = ("_registry", "_scope")

    def __init__(self, registry: AgentRegistry, scope: Scope) -> None:
        self._registry = registry
        self._scope = scope

    def register(self, runtime: AgentRuntime, *, replace: bool = False) -> AgentRuntime:
        """Register an agent for the lifetime of the plugin scope."""

        return self._registry.register(runtime, scope=self._scope, replace=replace)

    def unregister(self, name: str) -> bool:
        """Remove this scope's agent registration of ``name`` early.

        Identity-checked: it can never remove a successor's same-named runtime.
        """

        return self._registry.unregister(name, scope_id=self._scope.id)


def _budget_scope(environment: RunEnvironment) -> AbstractContextManager[None]:
    """Bind this run's budget as the active allocation for the duration of the run."""

    if environment.budget is None:
        return nullcontext()
    return budget_scope(environment.budget)


def _record_interrupts(
    harness: Harness, agent: str, thread_id: str | None, result: AgentResult
) -> None:
    """Append the interrupt values a run paused on to the recording, when attached.

    Recording is inert outside ``record`` mode, and interrupt values are recorded for
    attribution rather than replayed: a replayed run still pauses against the
    engine's own checkpointer.
    """

    from chassis.replay.models import BoundaryKind
    from chassis.replay.session import boundary_key

    session = harness.replay
    if session is None:
        return
    for index, interrupt in enumerate(result.interrupts):
        session.record(
            BoundaryKind.INTERRUPT,
            key=boundary_key("interrupt", agent, thread_id, index),
            request={
                "agent": agent,
                "thread_id": thread_id,
                "interrupt_id": interrupt.interrupt_id,
            },
            response=interrupt.value,
            generation_id=result.generation_id,
            run_id=result.run_id,
        )


def _definition_digest(runtime: AgentRuntime, run_context: HarnessRunContext) -> str | None:
    """Ask a runtime for its graph-definition digest, when it has one.

    Optional on purpose: a backend without compiled graphs simply contributes
    nothing to the snapshot.
    """

    digest = getattr(runtime, "definition_digest", None)
    if not callable(digest):
        return None
    value = digest(run_context)
    return value if isinstance(value, str) else None


def _with_attribution(
    result: AgentResult, agent: str, agent_revision: str | None, digest: str
) -> AgentResult:
    """Stamp the logical agent identity and revision the run executed under.

    A runtime referenced by ``AgentSpec.runtime_ref`` may report its own internal
    name; attribution belongs to the logical agent the caller selected, so the
    wrapper states it explicitly.
    """

    from dataclasses import replace

    return replace(
        result,
        agent=agent,
        agent_revision=agent_revision,
        metadata={**dict(result.metadata), "snapshot_digest": digest},
    )


def _with_duration(result: AgentResult, duration: float) -> AgentResult:
    if result.duration_seconds:
        return result
    from dataclasses import replace

    return replace(result, duration_seconds=duration)


def environment_budget(limits: BudgetLimits | None) -> BudgetGovernor | None:
    """Build the budget governor for one run, if any limits apply."""

    if limits is None or limits.is_unlimited:
        return None
    return BudgetGovernor(limits)
