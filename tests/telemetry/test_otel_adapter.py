"""The OpenTelemetry adapter transports the signal contract faithfully.

Span names and attributes arrive exactly as declared (G27), nesting follows the
ambient OpenTelemetry context, correlation attributes ride along, redaction
happens before export, and backend failures stay isolated. The tests use the
SDK's in-memory exporter — no collector, no network.
"""

from __future__ import annotations

from typing import Any

import pytest

from chassis import DATABASE, PluginContext, plugin
from chassis.secrets.redaction import SecretRedactor
from chassis.telemetry.base import SafeTelemetry
from chassis.telemetry.signals import SIGNALS, validate_signal
from chassis.testing import TestHarness, fake_tool
from chassis.tools import ToolRequest

pytest.importorskip("opentelemetry.sdk", reason="the opentelemetry extra is not installed")

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from chassis.telemetry import OpenTelemetryTelemetry


@plugin(name="otel-db", version="1.0.0", provides={"database": "1.0.0"})
async def db_plugin(ctx: PluginContext) -> None:
    ctx.capabilities.provide(DATABASE, "db-handle")


def exporter_and_telemetry() -> tuple[InMemorySpanExporter, OpenTelemetryTelemetry]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, OpenTelemetryTelemetry(provider.get_tracer("chassis-test"))


def exported(exporter: InMemorySpanExporter) -> list[Any]:
    return list(exporter.get_finished_spans())


async def test_spans_export_with_contract_names_and_attributes() -> None:
    exporter, telemetry = exporter_and_telemetry()

    async with (
        telemetry.span("harness.reconcile", {"harness": "t", "desired": 2}),
        telemetry.span("plugin.mount", {"plugin": "db", "entry_id": "db"}),
    ):
        pass

    spans = exported(exporter)
    assert [span.name for span in spans] == ["plugin.mount", "harness.reconcile"]
    for span in spans:
        assert span.name in SIGNALS
        assert not validate_signal(span.name, dict(span.attributes))
    assert dict(spans[1].attributes)["harness"] == "t"


async def test_parent_and_child_relationships_follow_the_ambient_context() -> None:
    exporter, telemetry = exporter_and_telemetry()

    async with (
        telemetry.span("harness.reconcile", {"harness": "t"}),
        telemetry.span("plugin.mount", {"plugin": "db", "entry_id": "db"}),
        telemetry.span("tool.execute", {"tool": "echo", "generation_id": "gen_1"}),
    ):
        pass

    spans = {span.name: span for span in exported(exporter)}
    assert spans["plugin.mount"].parent is not None
    assert spans["plugin.mount"].parent.span_id == spans["harness.reconcile"].context.span_id
    assert spans["tool.execute"].parent.span_id == spans["plugin.mount"].context.span_id


async def test_events_attach_to_the_active_span_or_stand_alone() -> None:
    exporter, telemetry = exporter_and_telemetry()
    published = {
        "generation_id": "gen_1",
        "sequence": 1,
        "previous": None,
        "plugins": 1,
    }

    async with telemetry.span("harness.reconcile", {"harness": "t"}):
        telemetry.event("generation.publish", published)
    telemetry.event("budget.exhausted", {"tool": "echo", "dimension": "tokens"})

    spans = exported(exporter)
    outer = next(span for span in spans if span.name == "harness.reconcile")
    assert [event.name for event in outer.events] == ["generation.publish"]
    standalone = next(span for span in spans if span.name == "budget.exhausted")
    assert standalone.end_time >= standalone.start_time
    assert not validate_signal("budget.exhausted", dict(standalone.attributes))


async def test_redaction_happens_before_export() -> None:
    secret = "sk-live-otel-secret-99"
    redactor = SecretRedactor([secret])
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = OpenTelemetryTelemetry(provider.get_tracer("chassis-test"), redactor=redactor)

    async with telemetry.span(
        "tool.execute", {"tool": "echo", "generation_id": "gen_1", "api_key": secret}
    ) as span:
        span.set_attributes({"note": f"endpoint={secret}"})
        span.record_error(RuntimeError(f"failed with {secret}"))

    span = exported(exporter)[0]
    assert dict(span.attributes)["api_key"] == "<redacted>"
    assert secret not in repr(dict(span.attributes))
    assert secret not in repr(span.events)


async def test_backend_failures_stay_isolated() -> None:
    class Broken:
        def span(self, name: str, attributes: Any = None) -> Any:
            raise RuntimeError("collector gone")

        def event(self, name: str, attributes: Any = None) -> None:
            raise RuntimeError("collector gone")

    safe = SafeTelemetry(Broken())
    async with safe.span("harness.reconcile", {"harness": "t"}):
        pass
    safe.event("budget.exhausted", {"tool": "echo", "dimension": "tokens"})

    assert safe.failures >= 2


async def test_the_adapter_works_through_the_harness_chain() -> None:
    exporter, telemetry = exporter_and_telemetry()
    harness = TestHarness(telemetry=telemetry)
    harness.install_tools(fake_tool("echo", result="hi", parameters={"text": (str, ...)}))
    await harness.start()
    try:
        generation = harness.current_generation
        assert generation is not None
        async with harness.acquire():
            await harness.tool_executor.execute(
                ToolRequest(name="echo", args={"text": "x"}),
                snapshot=harness.tool_snapshot(generation),
            )
    finally:
        await harness.stop()

    spans = exported(exporter)
    names = [span.name for span in spans]
    assert "harness.reconcile" in names
    assert "tool.execute" in names
    assert "harness.shutdown" in names
    for span in spans:
        assert not validate_signal(span.name, dict(span.attributes)), span.name
