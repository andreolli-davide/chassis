"""Small collection helpers shared by Chassis data types.

``FrozenDict`` exists because pydantic's ``frozen=True`` blocks attribute
assignment but not mutation of the containers a model holds. Desired state and
manifests must be immutable in both senses: they are hashed, compared, and
observed by runs, so a mutable nested mapping would let state change without any
lifecycle transition.

It subclasses ``dict`` deliberately: pydantic serializes dict subclasses without
warnings, which ``types.MappingProxyType`` does not.
"""

from __future__ import annotations

from typing import Any

__all__ = ["FrozenDict"]


class FrozenDict(dict[str, Any]):
    """A dict that raises on every mutating operation."""

    __slots__ = ()

    @staticmethod
    def _immutable(*args: Any, **kwargs: Any) -> None:
        raise TypeError("this mapping is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    setdefault = _immutable
    update = _immutable

    def __ior__(self, other: Any) -> FrozenDict:
        raise TypeError("this mapping is immutable")

    def popitem(self) -> tuple[str, Any]:
        raise TypeError("this mapping is immutable")

    def copy(self) -> dict[str, Any]:
        """Return a mutable copy, so callers can change a copy explicitly."""

        return dict(self)
