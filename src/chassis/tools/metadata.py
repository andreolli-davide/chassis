"""Harness metadata attached to a LangChain-compatible tool.

The tool itself stays a ``langchain-core`` object: Chassis wraps rather than
replaces it, so names, descriptions, schemas, and execution behaviour remain
upstream-compatible (invariant: no second tool ecosystem).

What Chassis adds is what langchain-core does not model: who owns the tool, what
permissions the harness must check before running it, whether it is safe to retry,
and how it should be bounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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
    metadata: dict[str, Any] = field(default_factory=dict)

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
