"""Permissions and grants.

A permission names a harness-mediated operation, optionally narrowed to a
resource pattern::

    filesystem.read
    filesystem.write:/workspace/output/**

Permissions are *not* a sandbox. In-process plugins are trusted code and can
bypass the harness entirely; permissions constrain what the harness will do on
behalf of a caller (typically a model-issued tool call).
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from chassis.core.errors import ConfigurationError

__all__ = ["Permission", "PermissionGrant"]


@dataclass(frozen=True, slots=True)
class Permission:
    """A required permission, optionally scoped to a resource pattern."""

    name: str
    resource: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ConfigurationError("permission name must not be empty")
        if self.name.startswith(":") or self.name.endswith(":"):
            raise ConfigurationError(
                "permission name must not contain an empty segment", permission=self.name
            )
        if self.resource is not None and not self.resource:
            raise ConfigurationError("permission resource must not be empty", permission=self.name)

    @classmethod
    def parse(cls, text: str) -> Permission:
        """Parse ``"name"`` or ``"name:resource"``."""

        if not text:
            raise ConfigurationError("permission text must not be empty")
        name, separator, resource = text.partition(":")
        permission = cls(name=name.strip(), resource=resource.strip() if separator else None)
        if not permission.name:
            raise ConfigurationError("permission name must not be empty", permission=text)
        return permission

    def __str__(self) -> str:
        return self.name if self.resource is None else f"{self.name}:{self.resource}"


@dataclass(frozen=True, slots=True)
class PermissionGrant:
    """A granted permission, optionally narrowed to a resource pattern.

    A grant without a resource pattern covers the permission at any resource. A
    grant with a pattern only covers resources matching it (glob semantics).
    """

    name: str
    resource: str | None = None

    @classmethod
    def parse(cls, text: str) -> PermissionGrant:
        permission = Permission.parse(text)
        return cls(name=permission.name, resource=permission.resource)

    def allows(self, permission: Permission) -> bool:
        """Whether this grant covers ``permission``."""

        if permission.name != self.name:
            return False
        if self.resource is None:
            return True
        if permission.resource is None:
            # A scoped grant cannot cover an unscoped request.
            return False
        return fnmatch.fnmatchcase(permission.resource, self.resource)

    def __str__(self) -> str:
        return self.name if self.resource is None else f"{self.name}:{self.resource}"
