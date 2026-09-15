from __future__ import annotations

import pytest

from chassis.telemetry import (
    NoopTelemetry,
    RecordingTelemetry,
    Span,
    Telemetry,
)


async def test_noop_telemetry_accepts_everything_and_records_nothing() -> None:
    telemetry = NoopTelemetry()

    async with telemetry.span("plugin.mount", {"plugin": "web-search"}) as span:
        span.set_attribute("state", "active")
        span.set_attributes({"owner": "plugin_1"})
        span.record_error(RuntimeError("ignored"))

    telemetry.event("generation.publish", {"generation_id": "gen_0001"})


async def test_recording_telemetry_keeps_spans_and_events() -> None:
    telemetry = RecordingTelemetry()

    async with telemetry.span("generation.build", {"sequence": 4}) as span:
        span.set_attribute("plugins", 3)
    telemetry.event("generation.publish", {"generation_id": "gen_0004"})

    assert telemetry.span_names() == ["generation.build"]
    record = telemetry.spans[0]
    assert record.attributes == {"sequence": 4, "plugins": 3}
    assert record.duration_seconds >= 0
    assert telemetry.event_names() == ["generation.publish"]
    assert telemetry.events[0].attributes["generation_id"] == "gen_0004"


async def test_recording_telemetry_records_errors_without_raising() -> None:
    telemetry = RecordingTelemetry()

    async with telemetry.span("plugin.mount") as span:
        span.record_error(RuntimeError("setup failed"))

    assert telemetry.spans[0].errors == ["RuntimeError: setup failed"]


async def test_span_is_closed_even_when_the_block_raises() -> None:
    telemetry = RecordingTelemetry()

    with pytest.raises(ValueError):
        async with telemetry.span("graph.compile"):
            raise ValueError("compile failed")

    assert telemetry.span_names() == ["graph.compile"]


def test_telemetry_implementations_satisfy_the_protocol() -> None:
    assert isinstance(NoopTelemetry(), Telemetry)
    assert isinstance(RecordingTelemetry(), Telemetry)


async def test_span_protocol_is_runtime_checkable() -> None:
    telemetry = RecordingTelemetry()

    async with telemetry.span("policy.decision") as span:
        assert isinstance(span, Span)


def test_recording_telemetry_can_be_cleared() -> None:
    telemetry = RecordingTelemetry()
    telemetry.event("one")
    telemetry.clear()

    assert telemetry.span_names() == []
    assert telemetry.event_names() == []
