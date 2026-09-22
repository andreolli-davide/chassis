"""Telemetry: the Chassis instrumentation boundary.

The protocol, the no-op backend, and the recording backend import nothing beyond
the core. The LangSmith backend is imported lazily: touching
:class:`LangSmithTelemetry` or :class:`LangSmithSpan` loads it, and the backend
itself only imports the ``langsmith`` SDK when tracing is enabled.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from chassis.telemetry.base import NoopSpan, NoopTelemetry, Span, TeeTelemetry, Telemetry
from chassis.telemetry.recording import RecordedEvent, RecordedSpan, RecordingTelemetry
from chassis.telemetry.signals import (
    ATTRIBUTE_ITEMS_LIMIT,
    ATTRIBUTE_STRING_LIMIT,
    CORRELATION_FIELDS,
    SIGNALS,
    SignalKind,
    SignalSpec,
    validate_signal,
)

if TYPE_CHECKING:
    from chassis.telemetry.langsmith import LangSmithSpan, LangSmithTelemetry
    from chassis.telemetry.otel import OpenTelemetrySpan, OpenTelemetryTelemetry

__all__ = [
    "ATTRIBUTE_ITEMS_LIMIT",
    "ATTRIBUTE_STRING_LIMIT",
    "CORRELATION_FIELDS",
    "SIGNALS",
    "LangSmithSpan",
    "LangSmithTelemetry",
    "NoopSpan",
    "NoopTelemetry",
    "OpenTelemetrySpan",
    "OpenTelemetryTelemetry",
    "RecordedEvent",
    "RecordedSpan",
    "RecordingTelemetry",
    "SignalKind",
    "SignalSpec",
    "Span",
    "TeeTelemetry",
    "Telemetry",
    "validate_signal",
]

_LAZY = {
    "LangSmithSpan": "chassis.telemetry.langsmith",
    "LangSmithTelemetry": "chassis.telemetry.langsmith",
    "OpenTelemetrySpan": "chassis.telemetry.otel",
    "OpenTelemetryTelemetry": "chassis.telemetry.otel",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module_name), name)
