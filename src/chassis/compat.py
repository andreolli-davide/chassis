"""Compatibility support: typed deprecations for the documented surface.

The compatibility policy (``docs/compatibility.md``) promises that a stable
public API is deprecated before it is removed, and that every deprecation names
the API, its replacement, the version that deprecated it, and the earliest
version that may remove it. This module is that mechanism: one typed warning
category and one small decorator/helper pair. It deliberately does no runtime
introspection of callers and never aliases moved names — a move is a migration
note plus a warning, not a shim.
"""

from __future__ import annotations

import functools
import inspect
import warnings
from collections.abc import Callable
from typing import Any, TypeVar

__all__ = ["ChassisDeprecationWarning", "deprecated", "warn_deprecated"]

T = TypeVar("T")


class ChassisDeprecationWarning(DeprecationWarning):
    """Typed warning raised when a deprecated Chassis API is used.

    Besides the rendered message, the warning carries the deprecation contract
    as structured attributes (``api``, ``replacement``, ``since``,
    ``remove_in``) so tooling can branch on it without parsing text.

    Args:
        message: Human-readable deprecation summary.
        api: The deprecated API, as documented.
        since: Version that deprecated the API.
        remove_in: Earliest version that may remove it.
        replacement: The replacement API, or ``None`` when there is none.
    """

    def __init__(
        self,
        message: str,
        /,
        *,
        api: str,
        since: str,
        remove_in: str,
        replacement: str | None = None,
    ) -> None:
        super().__init__(message)
        self.api = api
        self.since = since
        self.remove_in = remove_in
        self.replacement = replacement


def warn_deprecated(
    *,
    api: str,
    since: str,
    remove_in: str,
    replacement: str | None = None,
    stacklevel: int = 2,
) -> None:
    """Emit one :class:`ChassisDeprecationWarning` carrying the full contract.

    Use this for deprecations a decorator cannot express (a removed parameter, a
    changed default, a conditional path). ``stacklevel`` points at the caller by
    default so filters and logs name the code that must change.
    """

    guidance = (
        f"use {replacement} instead" if replacement is not None else "there is no replacement"
    )
    message = (
        f"{api} is deprecated since Chassis {since} and will be removed no earlier "
        f"than Chassis {remove_in}; {guidance}"
    )
    warnings.warn(
        ChassisDeprecationWarning(
            message,
            api=api,
            since=since,
            remove_in=remove_in,
            replacement=replacement,
        ),
        stacklevel=stacklevel,
    )


def deprecated(
    *,
    since: str,
    remove_in: str,
    replacement: str | None = None,
    name: str | None = None,
) -> Callable[[T], T]:
    """Mark a function, method, or class deprecated.

    The decorated callable raises one :class:`ChassisDeprecationWarning` per
    call — for a class, at construction — pointing at the caller's frame. The
    callable's behavior is otherwise unchanged.

    Args:
        since: Version that deprecated the API.
        remove_in: Earliest version that may remove it.
        replacement: The replacement API, or ``None`` when there is none.
        name: The API name to report; defaults to the documented
            ``module.QualName`` of the decorated object.
    """

    def decorate(obj: T) -> T:
        api_name = name or f"{obj.__module__}.{getattr(obj, '__qualname__', type(obj).__name__)}"

        def announce() -> None:
            warn_deprecated(
                api=api_name,
                since=since,
                remove_in=remove_in,
                replacement=replacement,
                stacklevel=4,
            )

        if isinstance(obj, type):
            original_init: Callable[..., None] = obj.__init__

            @functools.wraps(original_init)
            def init(self: Any, *args: Any, **kwargs: Any) -> None:
                announce()
                original_init(self, *args, **kwargs)

            cast_init: Any = init
            cast_obj: Any = obj
            cast_obj.__init__ = cast_init
            return obj

        original: Callable[..., Any] = obj  # type: ignore[assignment]

        if inspect.iscoroutinefunction(original):
            # An async callable stays a coroutine function after decoration:
            # introspection-based dispatch must keep working.
            @functools.wraps(original)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                announce()
                return await original(*args, **kwargs)

            cast_async_wrapper: Any = async_wrapper
            return cast_async_wrapper  # type: ignore[return-value]

        @functools.wraps(original)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            announce()
            return original(*args, **kwargs)

        cast_wrapper: Any = wrapper
        return cast_wrapper  # type: ignore[return-value]

    return decorate
