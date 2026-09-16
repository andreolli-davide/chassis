"""Secret access.

Plugins read secrets through a provider rather than the process environment, so
future providers (Vault, AWS Secrets Manager, 1Password) do not change plugin
code.

Secret *values* must never appear in logs, traces, snapshots, diagnostics, replay
records, or exception strings (invariant I11). Two mechanisms enforce that:

- :class:`SecretValue` refuses to render itself; a value leaves it only through an
  explicit :meth:`SecretValue.reveal` call at the point of use.
- :class:`~chassis.secrets.redaction.SecretRedactor` scrubs values out of text and
  structured payloads before they are emitted anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from chassis.core.errors import SecretResolutionError

__all__ = ["SecretProvider", "SecretValue"]


@dataclass(frozen=True, slots=True, repr=False)
class SecretValue:
    """A resolved secret that does not render itself."""

    name: str
    _value: str = ""

    @classmethod
    def of(cls, name: str, value: str) -> SecretValue:
        """Construct a secret value from resolved material."""

        if not name:
            raise SecretResolutionError("secret name must not be empty", secret=name)
        return cls(name=name, _value=value)

    def reveal(self) -> str:
        """Return the secret material. Call sites are auditable by design."""

        return self._value

    def __repr__(self) -> str:
        return f"SecretValue(name={self.name!r}, value=<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"

    def to_dict(self) -> dict[str, str]:
        """Diagnostic view: the name only."""

        return {"name": self.name, "value": "<redacted>"}


@runtime_checkable
class SecretProvider(Protocol):
    """Resolves secret names to values."""

    async def get(self, name: str) -> SecretValue:
        """Resolve ``name``.

        Raises:
            SecretResolutionError: the secret is unavailable. The error never
                carries the value.
        """

        ...

    async def get_optional(self, name: str) -> SecretValue | None:
        """Resolve ``name``, returning ``None`` when it is unavailable."""

        ...
