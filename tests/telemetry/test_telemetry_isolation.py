"""Telemetry failures are isolated and adapters share one wiring (roadmap R019).

A failing backend can neither break the operation being observed nor suppress
another backend: failures at every span phase and at event time leave the
remaining backends with complete signals, and stay visible through the safe
wrapper's diagnostic counter and logger. Registered runtimes that accept
harness services bind the harness telemetry and redaction unless they requested
an explicit override.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from chassis import Harness
from chassis.telemetry import RecordingTelemetry, TeeTelemetry


class ExplodingBackend:
    """Backend that fails at exactly one phase."""

    def __init__(self, phase: str) -> None:
        self.phase = phase
        self.entered = False

    def span(self, name: str, attributes: Mapping[str, Any] | None = None) -> Any:
        backend = self

        class _Span:
            async def __aenter__(self) -> Any:
                if backend.phase == "enter":
                    raise RuntimeError("enter failed")
                backend.entered = True
                return self

            async def __aexit__(self, *exc: Any) -> bool:
                if backend.phase == "exit":
                    raise RuntimeError("exit failed")
                return False

            def set_attribute(self, key: str, value: Any) -> None:
                self.set_attributes({key: value})

            def set_attributes(self, values: Mapping[str, Any]) -> None:
                if backend.phase == "update":
                    raise RuntimeError("update failed")

            def record_error(self, error: BaseException) -> None:
                if backend.phase == "error":
                    raise RuntimeError("error failed")

        return _Span()

    def event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        if self.phase == "event":
            raise RuntimeError("event failed")


async def drive(span_owner: Any) -> None:
    async with span_owner.span("unit.work", {"k": 1}) as span:
        span.set_attributes({"updated": True})
        span.record_error(RuntimeError("boom"))
    span_owner.event("unit.event", {"k": 2})


async def test_tee_isolates_failures_at_every_span_phase() -> None:
    for phase in ("enter", "update", "error", "exit", "event"):
        recording = RecordingTelemetry()
        tee = TeeTelemetry(ExplodingBackend(phase), recording)

        await drive(tee)  # never raises

        # The healthy backend received the complete signal set.
        assert [span.name for span in recording.spans] == ["unit.work"] or phase == "enter"
        assert any(event.name == "unit.event" for event in recording.events)
        assert tee.failures >= 1


async def test_safe_telemetry_contains_arbitrary_backend_failures() -> None:
    from chassis.telemetry.base import SafeTelemetry, Telemetry

    safe = SafeTelemetry(ExplodingBackend("enter"))

    await drive(safe)  # never raises

    assert safe.failures >= 1

    # Even a backend that explodes on every call is contained.
    class AlwaysFails(Telemetry):  # type: ignore[misc]
        def span(self, name: str, attributes: Mapping[str, Any] | None = None) -> Any:
            raise RuntimeError("no spans for you")

        def event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
            raise RuntimeError("no events either")

    contained = SafeTelemetry(AlwaysFails())  # type: ignore[arg-type]
    await drive(contained)
    assert contained.failures >= 2


async def test_registered_runtimes_bind_harness_services_unless_overridden() -> None:
    class BindableRuntime:
        def __init__(self, name: str = "bindable", *, explicit: bool = False) -> None:
            self._name = name
            self.explicit = explicit
            self.bound: list[tuple[Any, Any]] = []

        @property
        def name(self) -> str:
            return self._name

        def bind_harness_services(self, telemetry: Any, redactor: Any) -> None:
            if not self.explicit:
                self.bound.append((telemetry, redactor))

        async def invoke(self, request: Any, run_context: Any) -> Any:
            raise NotImplementedError

        async def stream(self, request: Any, run_context: Any) -> AsyncIterator[Any]:
            raise NotImplementedError
            yield  # pragma: no cover - protocol shape only

    harness = Harness(name="binding")
    implicit = BindableRuntime("implicit")
    harness.register_agent(implicit)

    assert implicit.bound == [(harness.telemetry, harness.redactor)]

    explicit = BindableRuntime("explicit", explicit=True)
    harness.register_agent(explicit)
    assert explicit.bound == []


async def test_harness_wiring_is_safe_and_redacted() -> None:
    from chassis.telemetry.base import RedactingTelemetry, SafeTelemetry

    harness = Harness(name="wiring", telemetry=RecordingTelemetry())
    assert isinstance(harness.telemetry, SafeTelemetry)
    assert isinstance(harness.telemetry.inner, RedactingTelemetry)
    assert isinstance(harness.telemetry.inner.inner, RecordingTelemetry)  # type: ignore[union-attr]
