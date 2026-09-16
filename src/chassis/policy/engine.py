"""Policy evaluation at harness-controlled boundaries.

Capability *availability* and *authorization* are different questions: a plugin
may provide filesystem access without every caller being permitted to delete
through it. The policy engine answers the second question, and the harness
consults it only where it actually mediates an operation.

Trust boundary: this governs what the harness performs. It is not a sandbox
against in-process plugin code (invariant I10).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from chassis.policy.permissions import Permission, PermissionGrant

__all__ = [
    "AllowAllPolicy",
    "DenyAllPolicy",
    "GrantPolicy",
    "PolicyEngine",
    "PolicyRequest",
    "PolicyResult",
]


@dataclass(frozen=True, slots=True)
class PolicyRequest:
    """One operation the harness is about to perform."""

    permission: Permission
    subject: str
    resource: str | None = None
    context: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "permission": str(self.permission),
            "subject": self.subject,
            "resource": self.resource,
        }


@dataclass(frozen=True, slots=True)
class PolicyResult:
    """Outcome of evaluating one request."""

    allowed: bool
    reason: str
    approval_required: bool = False
    matched_grant: PermissionGrant | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "approval_required": self.approval_required,
            "matched_grant": None if self.matched_grant is None else str(self.matched_grant),
        }


@runtime_checkable
class PolicyEngine(Protocol):
    """Decides whether the harness may perform an operation."""

    async def evaluate(self, request: PolicyRequest) -> PolicyResult:
        """Evaluate one request. Must be side-effect free with respect to policy state."""

        ...


class AllowAllPolicy:
    """Default policy: the harness performs every operation it is asked to.

    Chassis is a harness, not a security product; the default is explicit rather
    than implicit. Register a :class:`GrantPolicy` (or your own engine) when
    operations must be restricted.
    """

    async def evaluate(self, request: PolicyRequest) -> PolicyResult:
        return PolicyResult(allowed=True, reason="no policy restrictions configured")


class DenyAllPolicy:
    """Policy that refuses every request, useful for tests and locked-down modes."""

    async def evaluate(self, request: PolicyRequest) -> PolicyResult:
        return PolicyResult(allowed=False, reason="policy denies every operation")


class GrantPolicy:
    """Policy built from an explicit allow-list of grants.

    Args:
        grants: Granted permissions, as :class:`PermissionGrant` or strings such as
            ``"filesystem.read"`` or ``"filesystem.write:/workspace/**"``.
        approval_required: Permission names that are allowed only after an
            approval gate. The policy reports ``approval_required`` and leaves the
            gate to the caller.
    """

    def __init__(
        self,
        grants: Iterable[PermissionGrant | str] = (),
        *,
        approval_required: Iterable[str] = (),
    ) -> None:
        self._grants = tuple(
            grant if isinstance(grant, PermissionGrant) else PermissionGrant.parse(grant)
            for grant in grants
        )
        self._approval_required = frozenset(approval_required)

    @property
    def grants(self) -> tuple[PermissionGrant, ...]:
        return self._grants

    async def evaluate(self, request: PolicyRequest) -> PolicyResult:
        for grant in self._grants:
            if grant.allows(request.permission):
                if request.permission.name in self._approval_required:
                    return PolicyResult(
                        allowed=True,
                        reason=f"granted by {grant}, pending approval",
                        approval_required=True,
                        matched_grant=grant,
                    )
                return PolicyResult(
                    allowed=True,
                    reason=f"granted by {grant}",
                    matched_grant=grant,
                )
        return PolicyResult(
            allowed=False,
            reason=f"no grant covers {request.permission}",
        )
