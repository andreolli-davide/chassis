"""Redaction of secret material from anything Chassis emits.

Every boundary that produces text or structured payloads destined for logs,
traces, snapshots, diagnostics, replay records, or exception context runs it
through a :class:`SecretRedactor` first (invariant I11).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from chassis.secrets.base import SecretValue

__all__ = ["REDACTED", "SecretRedactor", "redact"]

#: Marker substituted for secret material.
REDACTED = "<redacted>"

#: Values shorter than this are not tracked: replacing one- or two-character
#: strings would corrupt unrelated text, and such values carry no secret entropy.
_MIN_SECRET_LENGTH = 4


class SecretRedactor:
    """Replaces known secret values with a marker.

    The redactor never stores secret *names*; it only needs the material to scrub.
    Longer values are replaced first so that a secret which contains another one
    cannot leave a fragment behind.
    """

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._values: set[str] = set()
        for value in values:
            self.add(value)

    def add(self, value: str) -> bool:
        """Track ``value`` for redaction. Returns whether it was accepted."""

        if not isinstance(value, str) or len(value) < _MIN_SECRET_LENGTH:
            return False
        self._values.add(value)
        return True

    def add_secret(self, secret: SecretValue) -> bool:
        """Track the material behind ``secret``."""

        return self.add(secret.reveal())

    def discard(self, value: str) -> None:
        """Stop tracking ``value``."""

        self._values.discard(value)

    def __len__(self) -> int:
        return len(self._values)

    def redact(self, text: str) -> str:
        """Replace every tracked secret in ``text``."""

        if not self._values:
            return text
        for value in sorted(self._values, key=len, reverse=True):
            if value in text:
                text = text.replace(value, REDACTED)
        return text

    def redact_value(self, value: Any) -> Any:
        """Recursively redact strings inside mappings, sequences, and tuples."""

        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, Mapping):
            return {
                self.redact(key) if isinstance(key, str) else key: self.redact_value(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            redacted = [self.redact_value(item) for item in value]
            if isinstance(value, tuple):
                return tuple(redacted)
            if isinstance(value, (set, frozenset)):
                return type(value)(redacted)
            return redacted
        return value


def redact(payload: Any, values: Iterable[str]) -> Any:
    """One-shot helper for redacting a payload against a set of secret values."""

    return SecretRedactor(values).redact_value(payload)
