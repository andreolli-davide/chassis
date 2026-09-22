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

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

__all__ = ["FrozenDict", "freeze", "frozen_mapping"]


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


def freeze(value: Any) -> Any:
    """Deep-freeze a JSON-compatible value for publication.

    Mappings become :class:`FrozenDict`, sequences become tuples, and sets become
    frozensets, recursively. Values that are not containers (plugin types,
    instances, scalars) are returned unchanged, so a caller can freeze a mapping
    whose values include callables. Freezing is what stops a control-plane
    authoring object from aliasing mutable state into a published revision.
    """

    if isinstance(value, Mapping):
        return FrozenDict({str(key): freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze(item) for item in value)
    return value


def frozen_mapping(value: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    """Deep-frozen, read-only mapping for published metadata containers.

    The outer proxy denies key assignment; the inner :func:`freeze` denies
    nested mutation and copies the payload, so author-owned containers can
    never alias published state.
    """

    return MappingProxyType(dict(freeze(value or {})))
