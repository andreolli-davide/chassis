from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from typing import Any

import pytest

from chassis.secrets import SecretRedactor
from chassis.telemetry import LangSmithSpan, LangSmithTelemetry
from chassis.testing import TestHarness

SECRET = "sk-live-abcdef123456"


class FakeRun:
    """Records what a LangSmith run would have received."""

    def __init__(self, name: str, metadata: Mapping[str, Any]) -> None:
        self.name = name
        self.metadata = dict(metadata)
        self.events: list[dict[str, Any]] = []

    def add_metadata(self, values: Mapping[str, Any]) -> None:
        self.metadata.update(values)

    def add_event(self, payload: Mapping[str, Any]) -> None:
        self.events.append(dict(payload))


class RecordingTracer:
    """Stands in for ``langsmith.trace``."""

    def __init__(self) -> None:
        self.runs: list[FakeRun] = []
        self.failures: int = 0

    @contextmanager
    def __call__(self, name: str, **kwargs: Any) -> Generator[FakeRun]:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("langsmith unavailable")
        run = FakeRun(name, kwargs.get("metadata") or {})
        self.runs.append(run)
        yield run


def wired(
    *, current: FakeRun | None = None, **kwargs: Any
) -> tuple[LangSmithTelemetry, RecordingTracer]:
    telemetry = LangSmithTelemetry(enabled=True, **kwargs)
    tracer = RecordingTracer()
    telemetry._trace = tracer  # type: ignore[assignment]
    telemetry._current_run = lambda: current  # type: ignore[assignment]
    return telemetry, tracer


def test_tracing_is_disabled_without_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)

    assert LangSmithTelemetry().enabled is False


def test_tracing_can_be_enabled_by_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    assert LangSmithTelemetry().enabled is True

    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    assert LangSmithTelemetry().enabled is False


async def test_disabled_telemetry_records_nothing_and_never_touches_the_sdk() -> None:
    telemetry = LangSmithTelemetry(enabled=False)

    async with telemetry.span("harness.reconcile", {"plugins": 2}) as span:
        span.set_attribute("generation_id", "gen_0001")

    telemetry.event("generation.publish", {"generation_id": "gen_0001"})


async def test_span_creates_a_run_with_redacted_metadata() -> None:
    telemetry, tracer = wired(redactor=SecretRedactor([SECRET]))

    async with telemetry.span(
        "plugin.mount", {"plugin": "web-search", "credential": SECRET}
    ) as span:
        span.set_attributes({"instance_id": "plugin_1"})

    run = tracer.runs[0]
    assert run.name == "plugin.mount"
    assert run.metadata["plugin"] == "web-search"
    assert run.metadata["credential"] == "<redacted>"
    assert run.metadata["instance_id"] == "plugin_1"
    assert SECRET not in str(run.metadata)


async def test_span_records_errors_as_metadata() -> None:
    telemetry, tracer = wired()

    async with telemetry.span("plugin.mount") as span:
        span.record_error(RuntimeError("setup exploded"))

    assert tracer.runs[0].metadata["chassis_error"] == "RuntimeError: setup exploded"


async def test_events_attach_to_the_ambient_run() -> None:
    run = FakeRun("agent.run", {})
    telemetry, _ = wired(current=run)

    telemetry.event("generation.drain", {"generation_id": "gen_0004"})

    assert run.events == [{"name": "generation.drain", "generation_id": "gen_0004"}]


async def test_events_without_an_ambient_run_are_dropped() -> None:
    telemetry, tracer = wired(current=None)

    telemetry.event("generation.publish", {"generation_id": "gen_0001"})

    assert tracer.runs == []


async def test_tracing_failure_does_not_break_the_caller() -> None:
    telemetry, tracer = wired()
    tracer.failures = 1
    reached: list[bool] = []

    async with telemetry.span("harness.reconcile") as span:
        span.set_attribute("plugins", 3)
        reached.append(True)

    # Entering the span failed, so the traced operation still ran against a no-op
    # span instead of aborting.
    assert reached == [True]
    assert tracer.runs == []


async def test_tracing_failure_is_recorded_when_it_must_be_surfaced() -> None:
    telemetry, tracer = wired(raise_on_error=True)
    tracer.failures = 1

    with pytest.raises(RuntimeError):
        async with telemetry.span("harness.reconcile"):
            pass  # pragma: no cover - the context manager raises on entry


def test_span_protocol_shape() -> None:
    run = FakeRun("x", {})
    span = LangSmithSpan(run)

    span.set_attribute("a", 1)
    span.set_attributes({"b": 2})

    assert run.metadata == {"a": 1, "b": 2}


async def test_harness_lifecycle_reaches_langsmith_when_enabled() -> None:
    telemetry, tracer = wired()

    async with TestHarness(telemetry=telemetry):
        pass

    names = [run.name for run in tracer.runs]
    assert "harness.reconcile" in names
    assert "harness.shutdown" in names
