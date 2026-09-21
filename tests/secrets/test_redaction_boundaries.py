"""Adversarial secret-leak matrix across every exported boundary (roadmap R003).

Every test asserts the *absence* of the secret material from an exported
representation, not merely the presence of a redaction marker in one output.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import contextmanager
from typing import Any, ClassVar

import pytest

from chassis import Harness, PluginContext, plugin
from chassis.capabilities import DATABASE, POLICY
from chassis.core.errors import AgentExecutionError, EffectCleanupError, PolicyDenied
from chassis.policy import PolicyRequest, PolicyResult
from chassis.replay import BoundaryKind, ReplayMode, ReplaySession
from chassis.runtime import AgentEvent, AgentRequest, AgentResult, HarnessRunContext
from chassis.secrets import REDACTED, SecretRedactor, SecretValue
from chassis.telemetry import LangSmithTelemetry, RecordingTelemetry
from chassis.tools import ToolPolicy, ToolRequest

SECRET = "sk-live-0123456789abcdef"
SHORT = "s3c"


@contextmanager
def trace_run(name: str, metadata: Mapping[str, Any]):  # type: ignore[no-untyped-def]
    run = FakeRun(name, metadata)
    FakeRun.created.append(run)
    yield run


class FakeRun:
    """Records what a LangSmith run would have received."""

    created: ClassVar[list[FakeRun]] = []

    def __init__(self, name: str, metadata: Mapping[str, Any]) -> None:
        self.name = name
        self.metadata = dict(metadata)
        self.events: list[dict[str, Any]] = []

    def add_metadata(self, values: Mapping[str, Any]) -> None:
        self.metadata.update(values)

    def add_event(self, payload: Mapping[str, Any]) -> None:
        self.events.append(dict(payload))


class VerbosePolicy:
    """Policy whose denial reason carries secret material."""

    async def evaluate(self, request: PolicyRequest) -> PolicyResult:
        return PolicyResult(allowed=False, reason=f"denied by backoffice ({SECRET})")


class ExplodingRuntime:
    """Agent runtime whose failure text carries secret material."""

    @property
    def name(self) -> str:
        return "exploder"

    async def invoke(self, request: AgentRequest, run_context: HarnessRunContext) -> AgentResult:
        raise RuntimeError(f"backend died with {SECRET}")

    async def stream(
        self, request: AgentRequest, run_context: HarnessRunContext
    ) -> AsyncIterator[AgentEvent]:
        raise RuntimeError(f"backend died with {SECRET}")
        yield AgentEvent(agent="exploder", generation_id="", run_id="", kind="end")


def policy_provider(name: str, engine: object):  # type: ignore[no-untyped-def]
    @plugin(name=name, version="1.0.0", provides={"policy": "1.0.0"})
    async def provide(ctx: PluginContext) -> None:
        ctx.capabilities.provide(POLICY, engine)

    return provide


def fetcher():  # type: ignore[no-untyped-def]
    from langchain_core.tools import tool

    @tool
    def fetch(url: str) -> str:
        """Fetch a URL."""

        return "fetched"

    @plugin(name="fetcher", version="1.0.0")
    async def fetcher_plugin(ctx: PluginContext) -> None:
        ctx.tools.register(
            fetch,
            policy=ToolPolicy(permissions=("network.fetch",)),
            metadata={
                "token": SECRET,
                "config": {"client_secret": SECRET, "note": f"leak {SECRET}"},
            },
        )

    return fetcher_plugin


def leaky_provider():  # type: ignore[no-untyped-def]
    @plugin(name="leaky", version="1.0.0", provides={"database": "1.0.0"})
    async def provide(ctx: PluginContext) -> None:
        ctx.capabilities.provide(
            DATABASE,
            "db",
            metadata={"api_key": SECRET, "note": f"leak {SECRET}"},
        )

        def dispose() -> None:
            raise RuntimeError(f"cleanup failed with {SECRET}")

        ctx.cleanup(f"dispose leaky ({SECRET})", dispose)

    return provide


async def call_fetch(harness: Harness) -> Any:
    generation = harness.current_generation
    assert generation is not None
    environment = harness.run_environment(generation)
    return await environment.executor.execute(
        ToolRequest(name="fetch", args={"url": "https://example.test"}),
        snapshot=harness.tool_snapshot(generation),
        policy=environment.policy,
    )


def test_nested_mappings_and_sequences_are_scrubbed_at_every_depth() -> None:
    redactor = SecretRedactor([SECRET])
    payload = {
        "outer": {"api_key": f"prefix-{SECRET}-suffix", "items": [{"token": SECRET}, "plain"]},
        "authorization": f"Bearer {SECRET}",
        "note": f"leak {SECRET}",
    }

    redacted = redactor.redact_value(payload)

    assert SECRET not in str(redacted)
    assert redacted["outer"]["items"][0]["token"] == REDACTED
    assert redacted["authorization"] == REDACTED
    assert redacted["note"] == f"leak {REDACTED}"
    assert redacted["outer"]["items"][1] == "plain"


def test_short_secret_values_are_protected_too() -> None:
    redactor = SecretRedactor()

    assert redactor.add(SHORT) is True
    assert redactor.redact(f"pin={SHORT} ok") == f"pin={REDACTED} ok"

    redactor.add_secret(SecretValue.of("pin", SHORT))
    assert SHORT not in str(redactor.redact_value({"note": f"leak {SHORT}"}))


async def test_policy_denial_reasons_are_sanitized() -> None:
    harness = Harness()
    harness.install(policy_provider("verbose", VerbosePolicy()), entry_id="verbose")
    harness.install(fetcher(), entry_id="fetcher")
    await harness.start()
    try:
        harness.redactor.add(SECRET)
        with pytest.raises(PolicyDenied) as excinfo:
            await call_fetch(harness)

        assert SECRET not in str(excinfo.value)
        assert SECRET not in str(excinfo.value.context)
    finally:
        await harness.stop()


async def test_agent_runtime_exceptions_are_sanitized_at_the_public_boundary() -> None:
    harness = Harness()
    harness.register_agent(ExplodingRuntime())
    await harness.start()
    try:
        harness.redactor.add(SECRET)

        with pytest.raises(AgentExecutionError) as excinfo:
            await harness.agents.invoke("exploder", {"messages": []})

        assert SECRET not in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, RuntimeError)

        with pytest.raises(AgentExecutionError) as stream_error:
            async for _ in harness.agents.stream("exploder", {"messages": []}):
                pass

        assert SECRET not in str(stream_error.value)
    finally:
        await harness.stop()


async def test_cleanup_failure_reports_are_sanitized() -> None:
    harness = Harness()
    harness.install(leaky_provider(), entry_id="leaky")
    await harness.start()
    harness.redactor.add(SECRET)

    with pytest.raises(EffectCleanupError) as excinfo:
        await harness.stop()

    assert SECRET not in str(excinfo.value)
    assert SECRET not in str(excinfo.value.to_dict())
    assert SECRET not in str(harness.diagnostics.status()["failures"])


async def test_span_attributes_updates_and_events_are_scrubbed() -> None:
    recorder = RecordingTelemetry()
    harness = Harness(telemetry=recorder)
    await harness.start()
    try:
        harness.redactor.add(SECRET)
        async with harness.telemetry.span(
            "custom.work", {"credential": SECRET, "note": f"leak {SECRET}"}
        ) as span:
            span.set_attributes({"nested": {"api_key": SECRET, "rows": [{"token": SECRET}]}})
            span.set_attribute("detail", f"leak {SECRET}")
            span.record_error(RuntimeError(f"boom {SECRET}"))
        harness.telemetry.event("custom.signal", {"password": SECRET, "rows": [f"leak {SECRET}"]})
    finally:
        await harness.stop()

    rendered = str([record.to_dict() for record in recorder.spans])
    rendered += str([event.to_dict() for event in recorder.events])

    assert SECRET not in rendered
    assert REDACTED in rendered


async def test_langsmith_span_updates_are_scrubbed() -> None:
    FakeRun.created.clear()
    telemetry = LangSmithTelemetry(enabled=True, redactor=SecretRedactor([SECRET]))
    telemetry._trace = lambda name, **kwargs: trace_run(name, kwargs.get("metadata") or {})  # type: ignore[assignment]

    async with telemetry.span("plugin.mount", {"credential": SECRET}) as span:
        span.set_attributes({"note": f"leak {SECRET}", "api_key": SECRET})
        span.record_error(RuntimeError(f"boom {SECRET}"))

    run = FakeRun.created[0]
    telemetry._current_run = lambda: run  # type: ignore[assignment]
    telemetry.event("custom.signal", {"password": SECRET})

    exported = str(run.metadata) + str(run.events)
    assert SECRET not in exported
    assert REDACTED in exported


async def test_externally_supplied_replay_sessions_are_scrubbed() -> None:
    session = ReplaySession.from_dict(
        {
            "mode": "record",
            "metadata": {"authorization": f"Bearer {SECRET}", "dataset": "demo"},
            "records": [
                {
                    "kind": "tool",
                    "sequence": 0,
                    "key": "k",
                    "request": {"args": {"password": SECRET}},
                    "response": {"content": f"leak {SECRET}"},
                }
            ],
        }
    )

    harness = Harness(redactor=SecretRedactor([SECRET]), replay=session)
    await harness.start()
    try:
        assert SECRET not in str(session.metadata)
        exported = session.to_dict()
        assert SECRET not in str(exported)
        assert exported["metadata"]["dataset"] == "demo"
    finally:
        await harness.stop()


async def test_replay_session_metadata_and_records_are_scrubbed_on_export() -> None:
    session = ReplaySession(mode=ReplayMode.RECORD, metadata={"token": SECRET})

    harness = Harness(replay=session)
    await harness.start()
    try:
        harness.redactor.add(SECRET)  # learned after the session was attached
        session.record(
            BoundaryKind.TOOL,
            key="k",
            request={"args": {"password": SECRET}},
            response={"content": f"leak {SECRET}"},
        )

        exported = session.to_dict()
        assert SECRET not in str(exported)
        assert SECRET not in str(session.metadata)
    finally:
        await harness.stop()


async def test_tool_and_capability_metadata_are_scrubbed_in_diagnostics() -> None:
    harness = Harness()
    harness.install(leaky_provider(), entry_id="leaky")
    harness.install(fetcher(), entry_id="fetcher")
    await harness.start()
    try:
        harness.redactor.add(SECRET)

        assert SECRET not in str(harness.diagnostics.tools())
        assert SECRET not in str(harness.diagnostics.capabilities())
        assert SECRET not in str(harness.diagnostics.status())
    finally:
        # The leaky disposer fails by design; stop() aggregates but still exits.
        with pytest.raises(EffectCleanupError):
            await harness.stop()
