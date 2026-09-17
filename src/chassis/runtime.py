"""The backend-agnostic run boundary.

This module defines what Chassis knows about *execution*: a run context that
carries an immutable generation view, the request/result/event shapes, and the
small :class:`AgentRuntime` protocol. It deliberately knows nothing about
LangGraph -- LangGraph belongs behind this boundary (``chassis.langgraph``), so the
lifecycle kernel stays usable with any execution backend.

A graph node obtains runtime-bound services from :class:`HarnessRunContext`; it
never resolves against "whatever generation happens to be current", so a run
observes one coherent environment for its whole lifetime.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from chassis.budget.governor import BudgetGovernor
from chassis.capabilities.keys import CapabilityKey
from chassis.capabilities.snapshot import CapabilitySnapshot
from chassis.hooks.registry import HookSnapshot
from chassis.policy.engine import PolicyEngine
from chassis.secrets.base import SecretProvider
from chassis.secrets.redaction import SecretRedactor
from chassis.telemetry.base import Telemetry
from chassis.tools.executor import ToolExecutor
from chassis.tools.registry import ToolSnapshot

if TYPE_CHECKING:
    # Imported for typing only: runtime generation is defined below the plugin
    # layer's dependency graph, and this module must not close that loop.
    from chassis.core.generation import RuntimeGeneration

__all__ = [
    "AgentEvent",
    "AgentRequest",
    "AgentResult",
    "AgentRuntime",
    "HarnessRunContext",
    "RunEnvironment",
]


@dataclass(frozen=True, slots=True)
class RunEnvironment:
    """Boundary services and immutable views resolved for one runtime generation.

    Assembled by the control plane when a run acquires a generation. Everything
    here is already generation-scoped: tools, hooks, capabilities, policy, and
    secrets all describe the same composition.
    """

    generation: RuntimeGeneration
    capabilities: CapabilitySnapshot
    tools: ToolSnapshot
    hooks: HookSnapshot
    executor: ToolExecutor
    policy: PolicyEngine
    secrets: SecretProvider
    telemetry: Telemetry
    redactor: SecretRedactor
    budget: BudgetGovernor | None = None

    @property
    def generation_id(self) -> str:
        return self.generation.generation_id


@dataclass(frozen=True, slots=True)
class HarnessRunContext:
    """Immutable context handed to an agent runtime and its graph nodes.

    Args:
        generation_id: Generation this run observes.
        run_id: Unique identifier of this run.
        capabilities: Immutable capability snapshot of the generation.
        agent: Name of the agent being executed.
        agent_revision: Revision of the agent composition this run executes
            against, when the agent was published through an ``AgentSpec``. A run
            remains associated with the revision selected when it started.
        environment: Generation-scoped boundary services. ``None`` only when an
            agent runtime is invoked without a harness (for example in unit tests
            of a graph's own logic).
        user_id: Requesting user, when known.
        tenant_id: Requesting tenant, when known.
        thread_id: Conversation/thread identity for durable execution.
        metadata: Free-form, non-secret request metadata.
    """

    generation_id: str
    run_id: str
    capabilities: CapabilitySnapshot
    agent: str = ""
    agent_revision: str | None = None
    environment: RunEnvironment | None = None
    user_id: str | None = None
    tenant_id: str | None = None
    thread_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def agent_identity(self) -> str | None:
        """``name@revision`` identity of the agent, when the run has one."""

        if not self.agent:
            return None
        if self.agent_revision is None:
            return self.agent
        return f"{self.agent}@{self.agent_revision}"

    @property
    def budget(self) -> BudgetGovernor | None:
        return None if self.environment is None else self.environment.budget

    @property
    def tools(self) -> ToolSnapshot | None:
        return None if self.environment is None else self.environment.tools

    @property
    def hooks(self) -> HookSnapshot | None:
        return None if self.environment is None else self.environment.hooks

    @property
    def executor(self) -> ToolExecutor | None:
        return None if self.environment is None else self.environment.executor

    def require_capability(self, capability: CapabilityKey | str) -> Any:
        """Return the provider object for a capability of this generation."""

        return self.capabilities.require(capability)

    @classmethod
    def new(
        cls,
        *,
        generation: RuntimeGeneration,
        environment: RunEnvironment | None = None,
        agent: str = "",
        agent_revision: str | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
        thread_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        run_id: str | None = None,
    ) -> HarnessRunContext:
        """Build a run context for one acquired generation."""

        return cls(
            generation_id=generation.generation_id,
            run_id=run_id or f"run_{uuid.uuid4().hex[:12]}",
            capabilities=generation.snapshot,
            agent=agent,
            agent_revision=agent_revision,
            environment=environment,
            user_id=user_id,
            tenant_id=tenant_id,
            thread_id=thread_id,
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        """Diagnostic view. Never contains secrets or request payloads."""

        return {
            "generation_id": self.generation_id,
            "run_id": self.run_id,
            "agent": self.agent,
            "agent_revision": self.agent_revision,
            "agent_identity": self.agent_identity,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "thread_id": self.thread_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class AgentRequest:
    """A request to execute an agent.

    Args:
        input: Input value for the execution engine (for LangGraph, the graph
            input such as ``{"messages": [...]}``).
        thread_id: Durable thread identity. Required for checkpointing and resume.
        resume: Value supplied to a previously interrupted run. When set, the
            runtime resumes rather than starting new work.
        checkpoint_id: Optional checkpoint to resume from.
        metadata: Non-secret metadata attached to the run.
    """

    input: Any = None
    thread_id: str | None = None
    resume: Any = None
    checkpoint_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_resume(self) -> bool:
        return self.resume is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "resume": self.is_resume,
            "checkpoint_id": self.checkpoint_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class AgentInterrupt:
    """An interrupt raised by the execution engine, awaiting a resume value."""

    value: Any = None
    interrupt_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "interrupt_id": self.interrupt_id}


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Outcome of one agent run."""

    agent: str
    generation_id: str
    run_id: str
    output: Any = None
    thread_id: str | None = None
    agent_revision: str | None = None
    interrupts: tuple[AgentInterrupt, ...] = ()
    duration_seconds: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def interrupted(self) -> bool:
        """Whether the run paused on an interrupt and can be resumed."""

        return bool(self.interrupts)

    def resume_values(self) -> tuple[Any, ...]:
        return tuple(item.value for item in self.interrupts)

    @property
    def messages(self) -> tuple[Any, ...]:
        """Messages from the run output, when the engine produced a message list."""

        if isinstance(self.output, Mapping):
            value = self.output.get("messages")
            if isinstance(value, (list, tuple)):
                return tuple(value)
        return ()

    @property
    def text(self) -> str | None:
        """Text of the most recent message carrying string content."""

        for message in reversed(self.messages):
            content = getattr(message, "content", None)
            if isinstance(content, str) and content:
                return content
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "agent_revision": self.agent_revision,
            "generation_id": self.generation_id,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "interrupted": self.interrupted,
            "duration_seconds": self.duration_seconds,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One streamed event from an agent run."""

    agent: str
    generation_id: str
    run_id: str
    kind: str
    data: Any = None
    agent_revision: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "agent_revision": self.agent_revision,
            "generation_id": self.generation_id,
            "run_id": self.run_id,
            "kind": self.kind,
        }


@runtime_checkable
class AgentRuntime(Protocol):
    """Minimal execution boundary.

    Kept deliberately small: Chassis does not pretend every backend shares
    identical durability semantics, so the protocol carries only what the harness
    needs to acquire a generation and hand it to an execution engine.
    """

    @property
    def name(self) -> str:
        """Agent name used for registration and invocation."""

        ...

    async def invoke(self, request: AgentRequest, run_context: HarnessRunContext) -> AgentResult:
        """Execute the agent once."""

        ...

    def stream(
        self, request: AgentRequest, run_context: HarnessRunContext
    ) -> AsyncIterator[AgentEvent]:
        """Execute the agent, emitting events as they occur."""

        ...


def monotonic() -> float:
    """Monotonic clock used for run durations."""

    return time.monotonic()
