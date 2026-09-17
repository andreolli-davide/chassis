"""The documented public surface must exist and stay importable.

Documentation that references an API which no longer exists is a defect; this test
is what keeps the two in step.
"""

from __future__ import annotations

import importlib

import pytest

DOCUMENTED = {
    "chassis": [
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
        "CapabilityRegistry",
        "CapabilityRequirement",
        "CapabilitySnapshot",
        "CapabilityVersionMismatch",
        "ChassisError",
        "ConfigurationError",
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
        "PluginCycleError",
        "PluginHealth",
        "PluginInstance",
        "PluginManifest",
        "PluginSetupError",
        "PluginState",
        "PolicyDenied",
        "ReconcileResult",
        "ReplayMismatch",
        "RuntimeGeneration",
        "Scope",
        "ScopeClosedError",
        "ScopeState",
        "ScopedCapabilities",
        "ToolExecutionError",
        "plugin",
    ],
    "chassis.langgraph": [
        "AgentDefinition",
        "GraphBuildInputs",
        "GraphCache",
        "GraphCacheKey",
        "LangGraphAgent",
        "harness_tool_node",
    ],
    "chassis.runtime": [
        "AgentEvent",
        "AgentRequest",
        "AgentResult",
        "AgentRuntime",
        "HarnessRunContext",
        "RunEnvironment",
    ],
    "chassis.tools": [
        "RegisteredTool",
        "ScopedTools",
        "Tool",
        "ToolExecutionResult",
        "ToolExecutor",
        "ToolNotFound",
        "ToolPolicy",
        "ToolRegistry",
        "ToolRequest",
        "ToolSnapshot",
    ],
    "chassis.policy": [
        "AllowAllPolicy",
        "DenyAllPolicy",
        "GrantPolicy",
        "Permission",
        "PermissionGrant",
        "PolicyRequest",
        "PolicyResult",
    ],
    "chassis.hooks": [
        "HookErrorPolicy",
        "HookEvent",
        "HookMode",
        "HookRegistry",
        "HookResult",
        "HookSnapshot",
    ],
    "chassis.budget": [
        "BudgetDimension",
        "BudgetEnforcement",
        "BudgetGovernor",
        "BudgetLimit",
        "BudgetLimits",
        "BudgetUsage",
    ],
    "chassis.secrets": [
        "EnvSecretProvider",
        "RedactingSecretProvider",
        "SecretProvider",
        "SecretRedactor",
        "SecretValue",
        "StaticSecretProvider",
        "redact",
    ],
    "chassis.telemetry": [
        "LangSmithTelemetry",
        "NoopTelemetry",
        "RecordedSpan",
        "RecordingTelemetry",
        "Span",
        "TeeTelemetry",
        "Telemetry",
    ],
    "chassis.persistence": [
        "RuntimeSnapshot",
        "canonical_json",
        "chassis_version",
        "hash_text",
        "prompt_hash",
        "schema_hash",
        "stable_hash",
        "tool_schema_hash",
    ],
    "chassis.replay": [
        "BoundaryKind",
        "ReplayChatModel",
        "ReplayFallback",
        "ReplayMode",
        "ReplayRecord",
        "ReplaySession",
        "boundary_key",
    ],
    "chassis.config": [
        "DesiredStateAction",
        "DesiredStateChange",
        "HarnessConfig",
        "InstalledEntry",
        "PluginCatalog",
        "PluginEntryConfig",
        "diff_desired_state",
        "load_config",
        "parse_config",
    ],
    "chassis.evaluation": ["agent_target", "composition_metadata", "evaluate_agent"],
    "chassis.testing": [
        "FakeChatModel",
        "FakePolicy",
        "FakeSecrets",
        "FakeTelemetry",
        "TestHarness",
        "fake_tool",
    ],
}


@pytest.mark.parametrize("module_name", sorted(DOCUMENTED))
def test_documented_names_are_importable(module_name: str) -> None:
    module = importlib.import_module(module_name)

    missing = [name for name in DOCUMENTED[module_name] if not hasattr(module, name)]

    assert missing == [], f"{module_name} is missing {missing}"


@pytest.mark.parametrize("module_name", sorted(DOCUMENTED))
def test_documented_names_are_exported(module_name: str) -> None:
    module = importlib.import_module(module_name)
    exported: set[str] = set(getattr(module, "__all__", ()))

    missing = [name for name in DOCUMENTED[module_name] if name not in exported]

    assert missing == [], f"{module_name}.__all__ is missing {missing}"


def test_core_does_not_import_langgraph() -> None:
    """The lifecycle kernel must stay independent of the execution engine."""

    import subprocess
    import sys

    script = (
        "import sys; import chassis; "
        "assert 'langgraph' not in sys.modules, 'chassis imported langgraph eagerly'; "
        "print(chassis.__version__)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
