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

import logging
from collections.abc import AsyncGenerator, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import Any, Protocol, runtime_checkable

from chassis.secrets.redaction import SecretRedactor

_LOGGER = logging.getLogger("chassis.telemetry")

__all__ = [
    "NoopSpan",
    "NoopTelemetry",
    "RedactingTelemetry",
    "SafeTelemetry",
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
    Failures in one backend do not prevent the others from receiving the signal,
    and a contained failure is announced to the *other* backends as a
    ``telemetry.failure`` event so a dead backend stays visible where telemetry
    still works.
    """

    def __init__(self, *backends: Telemetry) -> None:
        if not backends:
            raise ValueError("TeeTelemetry requires at least one backend")
        self._backends = backends
        self._safe = tuple(SafeTelemetry(backend) for backend in backends)
        self._announcing = False
        for position, safe in enumerate(self._safe):
            safe.set_failure_sink(self._announce_failure(position))

    def _announce_failure(self, position: int) -> Any:
        """Failure sink for one backend: notify its siblings exactly once."""

        def sink(operation: str, signal: str) -> None:
            if self._announcing:
                return
            self._announcing = True
            try:
                backend = type(self._backends[position]).__name__
                for other, safe in enumerate(self._safe):
                    if other != position:
                        safe.event(
                            "telemetry.failure",
                            {
                                "operation": operation,
                                "signal": signal,
                                "backend": backend,
                            },
                        )
            finally:
                self._announcing = False

        return sink

    @property
    def backends(self) -> tuple[Telemetry, ...]:
        return self._backends

    @property
    def failures(self) -> int:
        """Backend failures contained across every tee'd backend."""

        return sum(safe.failures for safe in self._safe)

    @asynccontextmanager
    async def span(self, name: str, attributes: Mapping[str, Any] | None = None):  # type: ignore[no-untyped-def]
        async with AsyncExitStack() as stack:
            spans = [
                await stack.enter_async_context(safe.span(name, attributes)) for safe in self._safe
            ]
            yield _FanOutSpan(tuple(spans))

    def event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        for safe in self._safe:
            safe.event(name, attributes)


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


class SafeTelemetry:
    """Failure-isolating wrapper for an arbitrary telemetry backend.

    Every call runs inside its own guard: a backend that raises can neither
    break the operation being observed nor suppress other backends. Failures
    stay visible through :attr:`failures` (a diagnostic counter) and the
    ``chassis.telemetry`` logger.
    """

    def __init__(self, inner: Telemetry) -> None:
        self._inner = inner
        self._failures = 0
        self._sink: Any = None

    @property
    def inner(self) -> Telemetry:
        return self._inner

    @property
    def failures(self) -> int:
        """How many backend calls failed and were contained."""

        return self._failures

    def set_failure_sink(self, sink: Any) -> None:
        """Register a callback ``sink(operation, signal)`` for contained failures.

        Used by :class:`TeeTelemetry` so a dead backend is announced to its
        siblings as a ``telemetry.failure`` event. The sink runs inside its own
        guard: it can neither break the observed operation nor recurse.
        """

        self._sink = sink

    def _note(self, phase: str, name: str) -> None:
        self._failures += 1
        _LOGGER.warning("telemetry backend failed during %s of %r", phase, name, exc_info=True)
        if self._sink is not None:
            try:
                self._sink(phase, name)
            except Exception:
                _LOGGER.warning("telemetry failure sink failed", exc_info=True)

    @asynccontextmanager
    async def span(
        self, name: str, attributes: Mapping[str, Any] | None = None
    ) -> AsyncGenerator[Span]:
        entered: Any = None
        try:
            entered = self._inner.span(name, attributes)
            inner_span = await entered.__aenter__()
        except Exception:
            self._note("span enter", name)
            yield NoopSpan()
            return
        safe = _SafeSpan(inner_span, self._note, name)
        try:
            yield safe
        except BaseException as error:
            try:
                await entered.__aexit__(type(error), error, error.__traceback__)
            except Exception:
                self._note("span exit", name)
            raise
        else:
            try:
                await entered.__aexit__(None, None, None)
            except Exception:
                self._note("span exit", name)

    def event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        try:
            self._inner.event(name, attributes)
        except Exception:
            self._note("event", name)


class _SafeSpan:
    """Span that contains every backend failure instead of propagating it."""

    __slots__ = ("_inner", "_name", "_note")

    def __init__(self, inner: Span, note: Any, name: str) -> None:
        self._inner = inner
        self._note = note
        self._name = name

    def set_attribute(self, key: str, value: Any) -> None:
        self.set_attributes({key: value})

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        try:
            self._inner.set_attributes(attributes)
        except Exception:
            self._note("span update", self._name)

    def record_error(self, error: BaseException) -> None:
        try:
            self._inner.record_error(error)
        except Exception:
            self._note("span error", self._name)


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
