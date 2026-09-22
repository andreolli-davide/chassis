"""Typed error model for Chassis.

Every error raised by Chassis public APIs derives from :class:`ChassisError` and
carries structured, machine-readable ``context``. Callers can therefore branch on
exception type without parsing message strings, while operators still get enough
detail to explain a lifecycle failure.

Secret hygiene: errors never format values on construction, and the structured
context is expected to contain identifiers and versions rather than payloads.
Telemetry and diagnostics redact the context again before emitting it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "AgentExecutionError",
    "BudgetExceeded",
    "CapabilityAmbiguous",
    "CapabilityNotFound",
    "CapabilityVersionMismatch",
    "ChassisError",
    "CleanupFailure",
    "ConfigurationError",
    "EffectCleanupError",
    "FormatError",
    "GenerationConflictError",
    "GraphBuildError",
    "HarnessStateError",
    "HookExecutionError",
    "PluginContractError",
    "PluginCycleError",
    "PluginDependencyError",
    "PluginLoadError",
    "PluginSetupError",
    "PolicyDenied",
    "ReplayMismatch",
    "ScopeClosedError",
    "SecretResolutionError",
    "ToolExecutionError",
    "UnknownLeaseError",
]


class ChassisError(Exception):
    """Base class for all Chassis errors.

    Args:
        message: Human-readable summary.
        context: Structured, non-secret detail describing the failure.
    """

    code: str = "chassis_error"

    def __init__(self, message: str, /, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = dict(context)

    def __str__(self) -> str:
        if not self.context:
            return self.message
        rendered = ", ".join(f"{key}={value!r}" for key, value in sorted(self.context.items()))
        return f"{self.message} ({rendered})"

    def to_dict(self) -> dict[str, Any]:
        """Return a structured representation suitable for diagnostics."""
        return {
            "error": type(self).__name__,
            "code": self.code,
            "message": self.message,
            "context": dict(self.context),
        }


@dataclass(frozen=True, slots=True)
class CleanupFailure:
    """A single failure observed while unwinding owned effects."""

    description: str
    error: BaseException

    def to_dict(self, *, sanitize: Callable[[str], str] | None = None) -> dict[str, Any]:
        """Structured, redacted public representation of the failure.

        The error is rendered as its type plus a scrubbed message — never a raw
        exception string — so reports cannot leak author-written text.
        """

        scrub: Callable[[str], str] = sanitize if sanitize is not None else (lambda text: text)
        return {
            "description": scrub(self.description),
            "error_type": type(self.error).__name__,
            "error": scrub(str(self.error)),
        }


class ScopeClosedError(ChassisError):
    """Raised when an effect is created through a scope that is closing or closed."""

    code = "scope_closed"


class EffectCleanupError(ChassisError):
    """Raised after scope teardown when one or more disposers failed.

    Cleanup always runs to completion; failures are aggregated rather than
    aborting the unwind at the first error. Rendered text is sanitized through
    the optional ``sanitize`` callable (the harness passes its redactor), while
    ``failures`` keeps the original exceptions as structured detail.
    """

    code = "effect_cleanup"

    def __init__(
        self,
        scope_name: str,
        failures: tuple[CleanupFailure, ...],
        *,
        sanitize: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(
            f"{len(failures)} cleanup failure(s) while closing scope {scope_name!r}",
            scope=scope_name,
            failures=len(failures),
        )
        self.scope_name = scope_name
        self.failures = failures
        self._sanitize = sanitize

    def __str__(self) -> str:
        scrub: Callable[[str], str] = self._sanitize or (lambda text: text)
        details = "; ".join(
            f"{scrub(failure.description)} -> {type(failure.error).__name__}: "
            f"{scrub(str(failure.error))}"
            for failure in self.failures
        )
        base = super().__str__()
        return f"{base}: {details}" if details else base


class PluginLoadError(ChassisError):
    """Raised when a plugin cannot be loaded at all (bad manifest, bad type)."""

    code = "plugin_load"


class PluginSetupError(ChassisError):
    """Raised when plugin setup fails and its partial effects have been reverted.

    Rollback cleanup failures are aggregated in ``cleanup_failures`` and counted
    in the structured context, so they are never hidden inside the failed scope.
    """

    code = "plugin_setup"

    def __init__(
        self,
        message: str,
        /,
        *,
        cleanup_failures: tuple[CleanupFailure, ...] = (),
        **context: Any,
    ) -> None:
        super().__init__(message, cleanup_failures=len(cleanup_failures), **context)
        self.cleanup_failures = cleanup_failures


class PluginDependencyError(ChassisError):
    """Raised when a plugin's required capabilities cannot be satisfied."""

    code = "plugin_dependency"


class PluginContractError(ChassisError):
    """Raised when effective registrations violate a plugin's manifest contract.

    The structured context names the provider, the promised contract, its actual
    registrations, the affected consumers, and the rollback result.
    """

    code = "plugin_contract"


class PluginCycleError(ChassisError):
    """Raised when capability dependencies contain a cycle."""

    code = "plugin_cycle"


class CapabilityAmbiguous(ChassisError):
    """Raised when a capability has more than one provider where one is required.

    Ambiguity is diagnosed rather than resolved arbitrarily; callers either
    disambiguate explicitly or observe the diagnostic.
    """

    code = "capability_ambiguous"


class CapabilityNotFound(ChassisError):
    """Raised when a required capability has no provider in the snapshot."""

    code = "capability_not_found"


class CapabilityVersionMismatch(ChassisError):
    """Raised when providers exist but none satisfy the version requirement."""

    code = "capability_version_mismatch"


class HarnessStateError(ChassisError):
    """Raised when a harness operation is invalid for the current lifecycle state."""

    code = "harness_state"


class ConfigurationError(ChassisError):
    """Raised when configuration is invalid or inconsistent.

    Covers declarative configuration and validated constructor options.
    """

    code = "configuration"


class HookExecutionError(ChassisError):
    """Raised when a hook fails under the ``raise`` error policy."""

    code = "hook_execution"


class PolicyDenied(ChassisError):
    """Raised when the policy engine denies a requested permission.

    This is not a sandbox: in-process plugins are trusted code.
    """

    code = "policy_denied"


class BudgetExceeded(ChassisError):
    """Raised when a budget dimension is exhausted at a harness boundary."""

    code = "budget_exceeded"


class ToolExecutionError(ChassisError):
    """Raised when a tool call fails through the harness execution boundary."""

    code = "tool_execution"


class SecretResolutionError(ChassisError):
    """Raised when a secret cannot be resolved.

    Must never include the secret value, only the secret name.
    """

    code = "secret_resolution"


class GenerationConflictError(ChassisError):
    """Raised on an illegal concurrent mutation of runtime generations."""

    code = "generation_conflict"


class UnknownLeaseError(ChassisError):
    """Raised when releasing a lease that is not outstanding.

    Covers an unknown lease id and a duplicate release of an already released
    lease. Accounting is never altered by such a release.
    """

    code = "unknown_lease"


class AgentExecutionError(ChassisError):
    """Raised when an agent runtime fails across the public boundary.

    The runtime's own exception is preserved as the internal cause; the public
    message and context carry only sanitized text.
    """

    code = "agent_execution"


class GraphBuildError(ChassisError):
    """Raised when a LangGraph agent definition cannot be compiled."""

    code = "graph_build"


class ReplayMismatch(ChassisError):
    """Raised when a recorded boundary does not match the replayed operation."""

    code = "replay_mismatch"


class FormatError(ChassisError):
    """Raised when a persisted payload cannot be read safely.

    Covers explicit format-version dispatch: a version newer than this release
    supports (``future_version``), a malformed version value
    (``malformed_version``), a payload that does not match its declared shape
    (``corrupted``), and a payload with no migration path to the supported
    version (``unmigratable``). The meaning of an unknown version or field is
    never guessed. ``context`` carries the machine-readable ``format``,
    ``reason``, ``found``, and ``supported`` values.
    """

    code = "format_version"
