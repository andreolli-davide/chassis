"""OpenTelemetry telemetry adapter (optional ``opentelemetry`` extra).

One more backend over the same core signal contract: Chassis spans become
OpenTelemetry spans and Chassis events become OpenTelemetry events on the active
span (or instantaneous spans when no span is active). Signal names and
attributes are exactly the declared contract
(:mod:`chassis.telemetry.signals`) — the adapter transports them, it does not
reshape them.

The adapter is never load-bearing. Installing it requires the
``opentelemetry`` extra (`pip install "chassis-harness[opentelemetry]"`);
importing :mod:`chassis.telemetry` without it is fine, and constructing the
adapter without it raises :class:`~chassis._optional.MissingExtraError` naming
the extra. Backend failures stay isolated exactly as for any backend: wrap it
(or a `TeeTelemetry` fan-out) in `SafeTelemetry`, which is what the harness
does for every configured backend.

Attributes and recorded errors are scrubbed by the adapter's own redactor
before they reach the exporter, so redaction holds even when the adapter is
used standalone:

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from chassis import Harness
from chassis.telemetry import OpenTelemetryTelemetry

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)

harness = Harness(telemetry=OpenTelemetryTelemetry(provider.get_tracer("chassis")))
```
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from chassis._optional import EXTRA_OPENTELEMETRY, require_extra
from chassis.secrets.redaction import SecretRedactor

__all__ = ["OpenTelemetrySpan", "OpenTelemetryTelemetry"]

_LOGGER = logging.getLogger("chassis.telemetry.otel")


class OpenTelemetrySpan:
    """Span backed by an OpenTelemetry span.

    Attribute updates and errors are scrubbed before they reach the exporter;
    a failure is recorded as a sanitized copy, so the original exception text
    never crosses the boundary.
    """

    __slots__ = ("_redactor", "_span")

    def __init__(self, span: Any, redactor: SecretRedactor) -> None:
        self._span = span
        self._redactor = redactor

    def set_attribute(self, key: str, value: Any) -> None:
        self.set_attributes({key: value})

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        self._span.set_attributes(self._redactor.redact_value(dict(attributes)))

    def record_error(self, error: BaseException) -> None:
        message = self._redactor.redact(str(error))
        sanitized = RuntimeError(f"{type(error).__name__}: {message}")
        self._span.record_exception(sanitized)
        try:
            from opentelemetry.trace import Status, StatusCode

            self._span.set_status(Status(StatusCode.ERROR, type(error).__name__))
        except Exception:  # pragma: no cover - defensive, status is best-effort
            _LOGGER.debug("failed to set the OpenTelemetry span status", exc_info=True)


class OpenTelemetryTelemetry:
    """Maps the Chassis signal contract onto OpenTelemetry.

    Args:
        tracer: An OpenTelemetry ``Tracer``. Defaults to the ambient tracer
            under the ``chassis`` instrumentation name, so a Chassis span nests
            inside whatever trace the application already has.
        redactor: Redactor applied to attributes and recorded errors before
            they reach the exporter. Defaults to a fresh redactor, which still
            scrubs sensitive attribute *keys*.
    """

    __slots__ = ("_redactor", "_tracer")

    def __init__(
        self,
        tracer: Any = None,
        *,
        redactor: SecretRedactor | None = None,
    ) -> None:
        require_extra(
            EXTRA_OPENTELEMETRY,
            "opentelemetry",
            purpose="the OpenTelemetry telemetry adapter",
        )
        self._redactor = redactor if redactor is not None else SecretRedactor()
        if tracer is not None:
            self._tracer = tracer
        else:
            from opentelemetry import trace

            self._tracer = trace.get_tracer("chassis")

    @property
    def tracer(self) -> Any:
        return self._tracer

    @asynccontextmanager
    async def span(
        self, name: str, attributes: Mapping[str, Any] | None = None
    ) -> AsyncGenerator[OpenTelemetrySpan]:
        scrubbed = self._redactor.redact_value(dict(attributes or {}))
        with self._tracer.start_as_current_span(name, attributes=scrubbed) as span:
            yield OpenTelemetrySpan(span, self._redactor)

    def event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        """Emit one event: on the active span, or as an instantaneous span.

        OpenTelemetry events belong to a span. When a span is active the event
        attaches to it; with no active span the event becomes a zero-duration
        span of the same name, so nothing is silently dropped.
        """

        from opentelemetry import trace

        scrubbed = self._redactor.redact_value(dict(attributes or {}))
        current = trace.get_current_span()
        if current.is_recording():
            current.add_event(name, attributes=scrubbed)
            return
        with self._tracer.start_as_current_span(name, attributes=scrubbed):
            pass
