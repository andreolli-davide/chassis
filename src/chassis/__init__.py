"""Chassis: a production-grade Python agent harness.

Chassis owns runtime composition and lifecycle -- plugins, capabilities, scoped
resources, reversible effects, immutable runtime generations -- and hands an
immutable view of that composition to an execution engine such as LangGraph.
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
from chassis.core.errors import (
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
    HookExecutionError,
    PluginCycleError,
    PluginDependencyError,
    PluginLoadError,
    PluginSetupError,
    PolicyDenied,
    ReplayMismatch,
    ScopeClosedError,
    SecretResolutionError,
    ToolExecutionError,
)
from chassis.core.generation import GenerationState, RuntimeGeneration
from chassis.core.generations import GenerationManager
from chassis.core.scope import EffectRecord, Scope, ScopeState
from chassis.diagnostics import Diagnostics
from chassis.harness import Harness, HarnessState, ReconcileResult
from chassis.plugins import (
    DependencyResolver,
    Plugin,
    PluginContext,
    PluginHealth,
    PluginInstance,
    PluginManifest,
    PluginState,
    ResolutionPlan,
    plugin,
)

try:
    __version__ = version("chassis")
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
    "ConfigurationError",
    "DependencyResolver",
    "Diagnostics",
    "EffectCleanupError",
    "EffectRecord",
    "GenerationConflictError",
    "GenerationManager",
    "GenerationState",
    "GraphBuildError",
    "Harness",
    "HarnessState",
    "HookExecutionError",
    "Plugin",
    "PluginContext",
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
    "RuntimeGeneration",
    "Scope",
    "ScopeClosedError",
    "ScopeState",
    "ScopedCapabilities",
    "SecretResolutionError",
    "ToolExecutionError",
    "__version__",
    "plugin",
]
