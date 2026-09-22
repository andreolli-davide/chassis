"""Chassis: transactional runtime composition for dynamic agent systems.

Chassis owns runtime composition and lifecycle -- plugins, capabilities, scoped
resources, reversible effects, immutable runtime generations, budgets, diagnostics --
and hands an immutable view of that composition to an execution engine such as
LangGraph.

The core imports without ``langgraph``, ``langchain-core``, or ``langsmith``; those
are optional extras reached through :mod:`chassis.langgraph`,
:mod:`chassis.replay`, :mod:`chassis.telemetry`, and :mod:`chassis.testing`.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from chassis.capabilities import (
    ARTIFACTS,
    DATABASE,
    MEMORY,
    MODEL,
    POLICY,
    SANDBOX,
    SCHEDULER,
    SECRETS,
    TOOLS,
    CapabilityKey,
    CapabilityRegistration,
    CapabilityRegistry,
    CapabilityRequirement,
    CapabilitySnapshot,
    ScopedCapabilities,
)
from chassis.composition import CompositionScope
from chassis.core.errors import (
    AgentExecutionError,
    BudgetExceeded,
    CapabilityAmbiguous,
    CapabilityNotFound,
    CapabilityVersionMismatch,
    ChassisError,
    CleanupFailure,
    ConfigurationError,
    EffectCleanupError,
    GenerationConflictError,
    GraphBuildError,
    HarnessStateError,
    HookExecutionError,
    PluginContractError,
    PluginCycleError,
    PluginDependencyError,
    PluginLoadError,
    PluginSetupError,
    PolicyDenied,
    ReplayMismatch,
    ScopeClosedError,
    SecretResolutionError,
    ToolExecutionError,
    UnknownLeaseError,
)
from chassis.core.generation import GenerationLease, GenerationState, RuntimeGeneration
from chassis.core.generations import GenerationManager
from chassis.core.scope import EffectRecord, Scope, ScopeState
from chassis.diagnostics import Diagnostics, GenerationPressureReport
from chassis.harness import Harness, HarnessState, ReconcileResult
from chassis.plugins import (
    Plugin,
    PluginContext,
    PluginHealth,
    PluginInstance,
    PluginManifest,
    PluginState,
    ResolutionPlan,
    plugin,
)
from chassis.runtime import (
    AgentEvent,
    AgentRequest,
    AgentResult,
    AgentRuntime,
    HarnessRunContext,
    RunEnvironment,
)

try:
    # The distribution is ``chassis-harness``; the import package stays ``chassis``.
    __version__ = version("chassis-harness")
except PackageNotFoundError:  # pragma: no cover - only when running from a source tree
    __version__ = "0.0.0"

__all__ = [
    "ARTIFACTS",
    "DATABASE",
    "MEMORY",
    "MODEL",
    "POLICY",
    "SANDBOX",
    "SCHEDULER",
    "SECRETS",
    "TOOLS",
    "AgentEvent",
    "AgentExecutionError",
    "AgentRequest",
    "AgentResult",
    "AgentRuntime",
    "BudgetExceeded",
    "CapabilityAmbiguous",
    "CapabilityKey",
    "CapabilityNotFound",
    "CapabilityRegistration",
    "CapabilityRegistry",
    "CapabilityRequirement",
    "CapabilitySnapshot",
    "CapabilityVersionMismatch",
    "ChassisError",
    "CleanupFailure",
    "CompositionScope",
    "ConfigurationError",
    "Diagnostics",
    "EffectCleanupError",
    "EffectRecord",
    "GenerationConflictError",
    "GenerationLease",
    "GenerationManager",
    "GenerationPressureReport",
    "GenerationState",
    "GraphBuildError",
    "Harness",
    "HarnessRunContext",
    "HarnessState",
    "HarnessStateError",
    "HookExecutionError",
    "Plugin",
    "PluginContext",
    "PluginContractError",
    "PluginCycleError",
    "PluginDependencyError",
    "PluginHealth",
    "PluginInstance",
    "PluginLoadError",
    "PluginManifest",
    "PluginSetupError",
    "PluginState",
    "PolicyDenied",
    "ReconcileResult",
    "ReplayMismatch",
    "ResolutionPlan",
    "RunEnvironment",
    "RuntimeGeneration",
    "Scope",
    "ScopeClosedError",
    "ScopeState",
    "ScopedCapabilities",
    "SecretResolutionError",
    "ToolExecutionError",
    "UnknownLeaseError",
    "__version__",
    "plugin",
]
