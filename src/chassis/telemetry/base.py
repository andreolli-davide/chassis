"""Telemetry protocol.

Chassis instruments the operations it owns -- plugin lifecycle, dependency
resolution, reconciliation, generation publication and draining, policy
decisions, budget exhaustion, graph build and cache events. Model, tool, and
graph internals are traced natively by LangGraph/LangChain/LangSmith; Chassis
does not rebuild that.

Anything emitted through this interface has already been redacted: telemetry
implementations must not be trusted to remove secrets themselves.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol, runtime_checkable

__all__ = ["NoopSpan", "NoopTelemetry", "Span", "Telemetry"]


@runtime_checkable
class Span(Protocol):
    """A unit of work with attributes and an error channel."""

    def set_attribute(self, key: str, value: Any) -> None:
        """Attach one attribute."""

        ...

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        """Attach several attributes."""

        ...

    def record_error(self, error: BaseException) -> None:
        """Record a failure on the span without changing control flow."""

        ...


@runtime_checkable
class Telemetry(Protocol):
    """Receives Chassis lifecycle and control-plane signals."""

    def span(
        self, name: str, attributes: Mapping[str, Any] | None = None
    ) -> AbstractAsyncContextManager[Span]:
        """Open a span for the duration of a ``with`` block."""

        ...

    def event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        """Record a point-in-time signal that has no duration."""

        ...


class NoopSpan:
    """Span that discards everything."""

    __slots__ = ()

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        return None

    def record_error(self, error: BaseException) -> None:
        return None


class NoopTelemetry:
    """Telemetry that records nothing.

    The default, so that observability failures can never affect runtime
    correctness unless a tracing backend is explicitly configured as required.
    """

    @asynccontextmanager
    async def span(self, name: str, attributes: Mapping[str, Any] | None = None):  # type: ignore[no-untyped-def]
        yield NoopSpan()

    def event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        return None
