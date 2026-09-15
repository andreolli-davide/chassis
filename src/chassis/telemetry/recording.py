"""In-memory telemetry used by tests and local debugging.

This is a real backend, not a mock: it records the same spans and events the
production integrations receive, which makes it the honest way to assert what
Chassis instruments.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

__all__ = ["RecordedEvent", "RecordedSpan", "RecordingTelemetry"]


@dataclass(slots=True)
class RecordedSpan:
    """One finished span."""

    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "attributes": dict(self.attributes),
            "errors": list(self.errors),
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True, slots=True)
class RecordedEvent:
    """One point-in-time signal."""

    name: str
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "attributes": dict(self.attributes)}


class _RecordingSpan:
    __slots__ = ("_record",)

    def __init__(self, record: RecordedSpan) -> None:
        self._record = record

    def set_attribute(self, key: str, value: Any) -> None:
        self._record.attributes[key] = value

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        self._record.attributes.update(attributes)

    def record_error(self, error: BaseException) -> None:
        self._record.errors.append(f"{type(error).__name__}: {error}")


class RecordingTelemetry:
    """Telemetry that keeps every span and event it receives."""

    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []
        self.events: list[RecordedEvent] = []

    @asynccontextmanager
    async def span(self, name: str, attributes: Mapping[str, Any] | None = None):  # type: ignore[no-untyped-def]
        record = RecordedSpan(name=name, attributes=dict(attributes or {}))
        started = time.monotonic()
        try:
            yield _RecordingSpan(record)
        finally:
            record.duration_seconds = time.monotonic() - started
            self.spans.append(record)

    def event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        self.events.append(RecordedEvent(name=name, attributes=dict(attributes or {})))

    # ----------------------------------------------------------------- queries

    def span_names(self) -> list[str]:
        return [record.name for record in self.spans]

    def event_names(self) -> list[str]:
        return [record.name for record in self.events]

    def spans_named(self, name: str) -> list[RecordedSpan]:
        return [record for record in self.spans if record.name == name]

    def clear(self) -> None:
        self.spans.clear()
        self.events.clear()

    def __repr__(self) -> str:
        return f"RecordingTelemetry(spans={len(self.spans)}, events={len(self.events)})"
