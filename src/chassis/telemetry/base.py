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

from collections.abc import AsyncGenerator, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import Any, Protocol, runtime_checkable

from chassis.secrets.redaction import SecretRedactor

__all__ = [
    "NoopSpan",
    "NoopTelemetry",
    "RedactingTelemetry",
    "Span",
    "TeeTelemetry",
    "Telemetry",
]


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


class TeeTelemetry:
    """Fans one signal out to several backends.

    Useful whenever two sinks must both see the same span -- recording for
    assertions while sending to LangSmith, or fanning out to OpenTelemetry.
    Failures in one backend do not prevent the others from receiving the signal.
    """

    def __init__(self, *backends: Telemetry) -> None:
        if not backends:
            raise ValueError("TeeTelemetry requires at least one backend")
        self._backends = backends

    @property
    def backends(self) -> tuple[Telemetry, ...]:
        return self._backends

    @asynccontextmanager
    async def span(self, name: str, attributes: Mapping[str, Any] | None = None):  # type: ignore[no-untyped-def]
        async with AsyncExitStack() as stack:
            spans = [
                await stack.enter_async_context(backend.span(name, attributes))
                for backend in self._backends
            ]
            yield _FanOutSpan(tuple(spans))

    def event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        for backend in self._backends:
            try:
                backend.event(name, attributes)
            except Exception:
                continue


class _FanOutSpan:
    """Span that forwards to every backend's span."""

    __slots__ = ("_spans",)

    def __init__(self, spans: tuple[Span, ...]) -> None:
        self._spans = spans

    def set_attribute(self, key: str, value: Any) -> None:
        self.set_attributes({key: value})

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        for span in self._spans:
            try:
                span.set_attributes(attributes)
            except Exception:
                continue

    def record_error(self, error: BaseException) -> None:
        for span in self._spans:
            try:
                span.record_error(error)
            except Exception:
                continue


class RedactingTelemetry:
    """Wraps a backend so everything emitted is scrubbed first.

    The single redaction boundary for telemetry: attributes, span updates,
    events, and recorded errors pass through the harness redactor on the way in,
    so no backend is trusted to remove secrets itself.
    """

    def __init__(self, inner: Telemetry, redactor: SecretRedactor) -> None:
        self._inner = inner
        self._redactor = redactor

    @property
    def inner(self) -> Telemetry:
        return self._inner

    @asynccontextmanager
    async def span(
        self, name: str, attributes: Mapping[str, Any] | None = None
    ) -> AsyncGenerator[Span]:
        scrubbed = self._redactor.redact_value(dict(attributes or {}))
        async with self._inner.span(name, scrubbed) as span:
            yield _RedactingSpan(span, self._redactor)

    def event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        self._inner.event(name, self._redactor.redact_value(dict(attributes or {})))


class _RedactingSpan:
    """Span that scrubs attributes and errors before forwarding."""

    __slots__ = ("_inner", "_redactor")

    def __init__(self, inner: Span, redactor: SecretRedactor) -> None:
        self._inner = inner
        self._redactor = redactor

    def set_attribute(self, key: str, value: Any) -> None:
        self.set_attributes({key: value})

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        self._inner.set_attributes(self._redactor.redact_value(dict(attributes)))

    def record_error(self, error: BaseException) -> None:
        # The backend renders the exception it is given, so it receives a
        # sanitized copy; the original never crosses the boundary.
        message = self._redactor.redact(str(error))
        self._inner.record_error(RuntimeError(f"{type(error).__name__}: {message}"))


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
