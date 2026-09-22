"""Harness metadata attached to a registered tool.

The tool itself stays the object the plugin registered: Chassis wraps rather than
replaces it, so names, descriptions, schemas, and execution behaviour remain
upstream-compatible (invariant: no second tool ecosystem). Chassis core does not
import a tool library; a tool only has to satisfy the structural contract in
:mod:`chassis.tools.registry`.

What Chassis adds is what a bare tool does not model: who owns the tool, what
permissions the harness must check before running it, whether it is safe to retry,
and how it should be bounded.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from chassis.core.collections import frozen_mapping
from chassis.core.errors import ConfigurationError
from chassis.policy.permissions import Permission

__all__ = ["ToolPolicy"]


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """Execution policy for one tool.

    Args:
        permissions: Permissions the harness must hold before executing, as
            ``"network.fetch"`` or ``"filesystem.write:/workspace/**"``.
        idempotent: Whether repeating the call is safe.
        side_effects: Free-form labels describing what the tool changes, used for
            approval and audit decisions.
        timeout_seconds: Per-call timeout. ``None`` inherits the executor default.
        cost_class: Coarse cost classification for budgeting and routing.
        approval_required: Whether a human approval gate applies before execution.
    """

    permissions: tuple[str, ...] = ()
    idempotent: bool = False
    side_effects: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    cost_class: str | None = None
    approval_required: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalize sequences and validate the policy at construction."""

        object.__setattr__(self, "permissions", tuple(self.permissions))
        object.__setattr__(self, "side_effects", tuple(self.side_effects))
        object.__setattr__(self, "metadata", frozen_mapping(self.metadata))
        timeout = self.timeout_seconds
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise ConfigurationError(
                    "timeout_seconds must be a positive finite number", timeout=timeout
                )
            if timeout <= 0 or timeout != timeout or timeout == float("inf"):
                raise ConfigurationError(
                    "timeout_seconds must be a positive finite number", timeout=timeout
                )
            object.__setattr__(self, "timeout_seconds", float(timeout))

    @property
    def permission_objects(self) -> tuple[Permission, ...]:
        return tuple(Permission.parse(text) for text in self.permissions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "permissions": list(self.permissions),
            "idempotent": self.idempotent,
            "side_effects": list(self.side_effects),
            "timeout_seconds": self.timeout_seconds,
            "cost_class": self.cost_class,
            "approval_required": self.approval_required,
        }
