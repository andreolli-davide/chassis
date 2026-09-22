from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from chassis import DATABASE, Harness, PluginContext, plugin
from chassis.core.errors import PolicyDenied, ReplayMismatch
from chassis.replay import (
    BoundaryKind,
    ReplayChatModel,
    ReplayFallback,
    ReplayMode,
    ReplaySession,
    boundary_key,
)
from chassis.runtime import (
    AgentEvent,
    AgentInterrupt,
    AgentRequest,
    AgentResult,
    HarnessRunContext,
)
from chassis.testing import FakeChatModel, TestHarness, fake_tool
from chassis.tools import ToolPolicy, ToolRequest

SECRET = "sk-live-abcdef123456"


def session(mode: ReplayMode, **kwargs: Any) -> ReplaySession:
    return ReplaySession(mode=mode, **kwargs)


async def test_live_mode_records_nothing() -> None:
    harness_session = session(ReplayMode.LIVE)

    assert harness_session.records == []
    assert harness_session.is_recording is False
    assert harness_session.is_replaying is False


async def test_tool_call_is_recorded_and_replayed_without_executing() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    recording = session(ReplayMode.RECORD)
    result = None

    async with TestHarness(replay=recording) as harness:
        harness.install_tools(
            fake_tool("echo", result="echoed", parameters={"text": (str, ...)}, calls=calls)
        )
        await harness.reconcile()
        generation = harness.current_generation
        assert generation is not None
        await harness.tool_executor.execute(
            ToolRequest(name="echo", args={"text": "hi"}),
            snapshot=harness.tool_snapshot(generation),
        )

    assert calls == [("echo", {"text": "hi"})]
    assert recording.counts()["tool"] == 1

    # Replaying answers from the record, so the tool body never runs again.
    replaying = ReplaySession(mode=ReplayMode.REPLAY, records=list(recording.records))
    async with TestHarness(replay=replaying) as harness:
        harness.install_tools(
            fake_tool("echo", result="SHOULD NOT RUN", parameters={"text": (str, ...)}, calls=calls)
        )
        await harness.reconcile()
        generation = harness.current_generation
        assert generation is not None

        result = await harness.tool_executor.execute(
            ToolRequest(name="echo", args={"text": "hi"}),
            snapshot=harness.tool_snapshot(generation),
        )

    assert result is not None
    assert result.content == "echoed"
    assert calls == [("echo", {"text": "hi"})]


async def test_replay_of_a_different_tool_call_is_refused() -> None:
    recording = session(ReplayMode.RECORD)
    recording.record(
        BoundaryKind.TOOL,
        key=boundary_key(BoundaryKind.TOOL.value, "echo", {"text": "hi"}),
        request={"tool": "echo", "args": {"text": "hi"}},
        response={"name": "echo", "status": "ok", "content": "echoed"},
    )

    replaying = ReplaySession(mode=ReplayMode.REPLAY, records=list(recording.records))

    with pytest.raises(ReplayMismatch) as excinfo:
        replaying.replay(
            BoundaryKind.TOOL, key=boundary_key(BoundaryKind.TOOL.value, "echo", {"text": "other"})
        )

    assert excinfo.value.context["kind"] == "tool"
    assert excinfo.value.context["recorded"] == 1


async def test_unrecorded_tool_call_fails_by_default_and_can_run_live() -> None:
    replaying = ReplaySession(mode=ReplayMode.REPLAY)
    calls: list[tuple[str, dict[str, Any]]] = []
    result = None

    async with TestHarness(replay=replaying) as harness:
        harness.install_tools(
            fake_tool("echo", result="live", parameters={"text": (str, ...)}, calls=calls)
        )
        await harness.reconcile()
        generation = harness.current_generation
        assert generation is not None

        with pytest.raises(ReplayMismatch):
            await harness.tool_executor.execute(
                ToolRequest(name="echo", args={"text": "hi"}),
                snapshot=harness.tool_snapshot(generation),
            )
        assert calls == []

        replaying.fallback = ReplayFallback.LIVE
        result = await harness.tool_executor.execute(
            ToolRequest(name="echo", args={"text": "hi"}),
            snapshot=harness.tool_snapshot(generation),
        )

    assert result is not None
    assert result.content == "live"
    assert calls == [("echo", {"text": "hi"})]


async def test_replay_still_enforces_policy() -> None:
    """A recording answers what a tool returned, never whether it was allowed."""

    from chassis.core.errors import PolicyDenied
    from chassis.testing import FakePolicy
    from chassis.tools import ToolPolicy

    recording = session(ReplayMode.RECORD)
    recording.record(
        BoundaryKind.TOOL,
        key=boundary_key(BoundaryKind.TOOL.value, "rm", {"path": "/tmp/x"}),
        request={"tool": "rm", "args": {"path": "/tmp/x"}},
        response={"name": "rm", "status": "ok", "content": "deleted"},
    )
    replaying = ReplaySession(mode=ReplayMode.REPLAY, records=list(recording.records))
    calls: list[tuple[str, dict[str, Any]]] = []
    result = None

    async with TestHarness(policy=FakePolicy(["network.fetch"]), replay=replaying) as harness:
        harness.install_tools(
            fake_tool("rm", result="deleted", parameters={"path": (str, ...)}, calls=calls),
            policies={"rm": ToolPolicy(permissions=("filesystem.delete",))},
        )
        await harness.reconcile()
        generation = harness.current_generation
        assert generation is not None

        with pytest.raises(PolicyDenied):
            await harness.tool_executor.execute(
                ToolRequest(name="rm", args={"path": "/tmp/x"}),
                snapshot=harness.tool_snapshot(generation),
            )

    assert calls == []

    # Granting the permission lets the recording answer instead.
    async with TestHarness(policy=FakePolicy(["filesystem.delete"]), replay=replaying) as harness:
        harness.install_tools(
            fake_tool("rm", result="deleted", parameters={"path": (str, ...)}, calls=calls),
            policies={"rm": ToolPolicy(permissions=("filesystem.delete",))},
        )
        await harness.reconcile()
        generation = harness.current_generation
        assert generation is not None

        result = await harness.tool_executor.execute(
            ToolRequest(name="rm", args={"path": "/tmp/x"}),
            snapshot=harness.tool_snapshot(generation),
        )

    assert result is not None
    assert result.content == "deleted"
    assert calls == []


async def test_model_boundary_records_and_replays() -> None:
    recording = session(ReplayMode.RECORD)
    inner = FakeChatModel(responses=["first answer", "second answer"])
    model = ReplayChatModel(session=recording, inner=inner, model_name="fake-model")

    from langchain_core.messages import HumanMessage

    first = await model.ainvoke([HumanMessage("hi")])
    assert first.content == "first answer"
    assert recording.counts() == {"model": 1}

    replaying = ReplaySession(mode=ReplayMode.REPLAY, records=list(recording.records))
    replayed_model = ReplayChatModel(session=replaying, inner=None, model_name="fake-model")

    assert (await replayed_model.ainvoke([HumanMessage("hi")])).content == "first answer"


async def test_model_replay_refuses_a_different_request() -> None:
    recording = session(ReplayMode.RECORD)
    model = ReplayChatModel(
        session=recording, inner=FakeChatModel(responses=["answer"]), model_name="fake-model"
    )

    from langchain_core.messages import HumanMessage

    await model.ainvoke([HumanMessage("hi")])

    replaying = ReplaySession(mode=ReplayMode.REPLAY, records=list(recording.records))
    replayed_model = ReplayChatModel(session=replaying, inner=None, model_name="fake-model")

    with pytest.raises(ReplayMismatch):
        await replayed_model.ainvoke([HumanMessage("different")])


async def test_model_replay_refuses_a_different_model_identity() -> None:
    recording = session(ReplayMode.RECORD)
    model = ReplayChatModel(
        session=recording, inner=FakeChatModel(responses=["answer"]), model_name="model-a"
    )

    from langchain_core.messages import HumanMessage

    await model.ainvoke([HumanMessage("hi")])

    replaying = ReplaySession(mode=ReplayMode.REPLAY, records=list(recording.records))
    other = ReplayChatModel(session=replaying, inner=None, model_name="model-b")

    with pytest.raises(ReplayMismatch):
        await other.ainvoke([HumanMessage("hi")])


def test_recordings_round_trip_through_json(tmp_path: Path) -> None:
    original = session(ReplayMode.RECORD, metadata={"dataset": "smoke"})
    original.record_snapshot({"generation_id": "gen_0001", "plugins": {"a": "1.0.0"}})
    original.record_lifecycle("generation.publish", {"generation_id": "gen_0001"})
    original.record(
        BoundaryKind.TOOL,
        key="abc",
        request={"tool": "echo"},
        response={"content": "ok"},
    )

    path = original.save(tmp_path / "recording.json")
    restored = ReplaySession.load(path, mode=ReplayMode.REPLAY)

    assert restored.mode is ReplayMode.REPLAY
    assert restored.metadata == {"dataset": "smoke"}
    assert [record.key for record in restored.records] == [
        record.key for record in original.records
    ]
    assert restored.counts() == {"snapshot": 1, "lifecycle": 1, "tool": 1}


def test_sensitive_request_fields_are_redacted_in_recordings() -> None:
    recording = session(
        ReplayMode.RECORD,
        redactor=__import__("chassis.secrets", fromlist=["SecretRedactor"]).SecretRedactor(
            [SECRET]
        ),
    )

    record = recording.record(
        BoundaryKind.TOOL,
        key="k",
        request={"tool": "call", "api_key": SECRET, "endpoint": SECRET},
        response="ok",
    )

    assert record is not None
    assert "api_key" in record.request
    assert record.request["api_key"] == "<redacted>"
    assert record.response == "ok"
    assert SECRET not in str(record.to_dict())


async def test_harness_records_lifecycle_and_snapshot_boundaries() -> None:
    recording = session(ReplayMode.RECORD)

    @plugin(name="db", version="1.0.0", provides={"database": "1.0.0"})
    async def db(ctx: PluginContext) -> None:
        ctx.capabilities.provide(DATABASE, "db")

    harness = Harness(replay=recording)
    harness.install(db, entry_id="db")
    await harness.start()
    await harness.stop()

    lifecycle = [
        record.request["event"]
        for record in recording.records
        if record.kind is BoundaryKind.LIFECYCLE
    ]
    assert lifecycle == [
        "plugin.mount",
        "generation.publish",
        "harness.shutdown",
        "plugin.unmount",
    ]

    snapshots = [record for record in recording.records if record.kind is BoundaryKind.SNAPSHOT]
    assert snapshots[-1].response["generation_id"] == "gen_0001"
    assert snapshots[-1].response["plugins"]


async def test_interrupt_values_are_recorded_with_the_run_that_paused() -> None:
    recording = session(ReplayMode.RECORD)

    class PausingRuntime:
        name = "pausing"

        async def invoke(
            self, request: AgentRequest, run_context: HarnessRunContext
        ) -> AgentResult:
            return AgentResult(
                agent="pausing",
                generation_id=run_context.generation_id,
                run_id=run_context.run_id,
                output={},
                thread_id=request.thread_id,
                interrupts=(AgentInterrupt(value={"question": "approve?"}),),
            )

        async def stream(
            self, request: AgentRequest, run_context: HarnessRunContext
        ) -> AsyncIterator[AgentEvent]:
            yield AgentEvent(
                agent=self.name,
                generation_id=run_context.generation_id,
                run_id=run_context.run_id,
                kind="values",
            )

    harness = Harness(replay=recording)
    await harness.start()
    try:
        harness.register_agent(PausingRuntime())
        result = await harness.agents.invoke("pausing", {"messages": []}, thread_id="t-1")
    finally:
        await harness.stop()

    interrupts = [record for record in recording.records if record.kind is BoundaryKind.INTERRUPT]
    assert len(interrupts) == 1
    assert interrupts[0].response == {"question": "approve?"}
    assert interrupts[0].generation_id == result.generation_id
    assert interrupts[0].request["thread_id"] == "t-1"


def test_session_counts_group_by_boundary_kind() -> None:
    recording = session(ReplayMode.RECORD)
    recording.record(BoundaryKind.TOOL, key="a")
    recording.record(BoundaryKind.TOOL, key="b")
    recording.record(BoundaryKind.MODEL, key="c")

    assert recording.counts() == {"model": 1, "tool": 2}


# --------------------------------------------------------------------------
# Semantic completeness (R015): keys cover every option, results round-trip.
# --------------------------------------------------------------------------


def messages(text: str = "hi") -> list[Any]:
    from langchain_core.messages import HumanMessage

    return [HumanMessage(content=text)]


async def test_requests_differing_by_one_option_do_not_collide() -> None:
    recording = session(ReplayMode.RECORD)
    record_model = ReplayChatModel(
        session=recording, inner=FakeChatModel(responses=["cold", "hot"]), model_name="m"
    )
    record_model._generate(messages(), temperature=0.0)  # type: ignore[call-arg]
    record_model._generate(messages(), temperature=0.7)  # type: ignore[call-arg]

    recording.mode = ReplayMode.REPLAY
    replay_model = ReplayChatModel(session=recording, model_name="m")

    # Replaying in the opposite order still pairs each request with its own
    # result: the key includes the option that differs.
    hot = replay_model._generate(messages(), temperature=0.7)  # type: ignore[call-arg]
    cold = replay_model._generate(messages(), temperature=0.0)  # type: ignore[call-arg]

    assert hot.generations[0].message.content == "hot"
    assert cold.generations[0].message.content == "cold"


async def test_one_semantic_option_is_enough_to_mismatch() -> None:
    recording = session(ReplayMode.RECORD)
    record_model = ReplayChatModel(
        session=recording, inner=FakeChatModel(responses=["text"]), model_name="m"
    )
    record_model._generate(messages(), response_format={"type": "text"})  # type: ignore[call-arg]

    recording.mode = ReplayMode.REPLAY
    replay_model = ReplayChatModel(session=recording, model_name="m")

    with pytest.raises(ReplayMismatch):
        replay_model._generate(messages(), response_format={"type": "json"})  # type: ignore[call-arg]
    with pytest.raises(ReplayMismatch):
        replay_model._generate(messages(), stop=["x"])  # type: ignore[call-arg]
    with pytest.raises(ReplayMismatch):
        replay_model._generate(messages(), provider_options={"region": "eu"})  # type: ignore[call-arg]


async def test_stop_sequences_are_normalized_in_the_key() -> None:
    recording = session(ReplayMode.RECORD)
    record_model = ReplayChatModel(
        session=recording, inner=FakeChatModel(responses=["stopped"]), model_name="m"
    )
    record_model._generate(messages(), stop=["y", "x", "y"])  # type: ignore[call-arg]

    recording.mode = ReplayMode.REPLAY
    replay_model = ReplayChatModel(session=recording, model_name="m")

    result = replay_model._generate(messages(), stop=["x", "y"])  # type: ignore[call-arg]
    assert result.generations[0].message.content == "stopped"


async def test_non_canonical_options_are_rejected_not_omitted() -> None:
    recording = session(ReplayMode.RECORD)
    model = ReplayChatModel(session=recording, inner=FakeChatModel(responses=["x"]), model_name="m")

    with pytest.raises(ReplayMismatch):
        model._generate(messages(), temperature=object())  # type: ignore[call-arg]
    with pytest.raises(ReplayMismatch):
        model._generate(messages(), temperature=float("nan"))  # type: ignore[call-arg]
    assert recording.records == []


async def test_recorded_results_preserve_llm_output_and_generation_metadata() -> None:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    class RichResultModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "rich"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(content="hi"),
                        generation_info={"finish_reason": "stop"},
                    )
                ],
                llm_output={"finish_stats": {"calls": 7}},
            )

    recording = session(ReplayMode.RECORD)
    record_model = ReplayChatModel(session=recording, inner=RichResultModel(), model_name="rich")
    record_model._generate(messages())  # type: ignore[call-arg]

    recording.mode = ReplayMode.REPLAY
    replay_model = ReplayChatModel(session=recording, model_name="rich")
    replayed = replay_model._generate(messages())  # type: ignore[call-arg]

    assert replayed.llm_output == {"finish_stats": {"calls": 7}}
    assert replayed.generations[0].generation_info == {"finish_reason": "stop"}


async def test_replay_presence_is_cursor_aware() -> None:
    recording = session(ReplayMode.RECORD, fallback=ReplayFallback.LIVE)
    record_model = ReplayChatModel(
        session=recording, inner=FakeChatModel(responses=["first"]), model_name="m"
    )
    record_model._generate(messages())  # type: ignore[call-arg]

    key = recording.records[0].key
    assert recording.has(BoundaryKind.MODEL, key=key) is True
    assert recording.has_remaining(BoundaryKind.MODEL, key=key) is True

    recording.mode = ReplayMode.REPLAY
    live_model = ReplayChatModel(
        session=recording, inner=FakeChatModel(responses=["second"]), model_name="m"
    )

    first = live_model._generate(messages())  # type: ignore[call-arg]
    assert first.generations[0].message.content == "first"

    # The record is exhausted: presence is cursor-aware, and the next call
    # falls back to live execution instead of re-answering or erroring.
    assert recording.has_remaining(BoundaryKind.MODEL, key=key) is False
    assert recording.peek(BoundaryKind.MODEL, key=key) is None
    assert recording.has(BoundaryKind.MODEL, key=key) is True

    second = live_model._generate(messages())  # type: ignore[call-arg]
    assert second.generations[0].message.content == "second"


# --------------------------------------------------------------------------
# The live boundary (R016): replayed calls run it identically.
# --------------------------------------------------------------------------


async def record_one_call(
    recording: ReplaySession,
    *,
    generation_id: str = "gen-old",
    run_id: str = "run-old",
    tool_call_id: str = "call-old",
    calls: list[Any] | None = None,
) -> Any:
    from chassis.testing import fake_tool

    async with TestHarness(replay=recording) as harness:
        harness.install_tools(
            fake_tool("echo", result="echoed", parameters={"text": (str, ...)}, calls=calls)
        )
        await harness.reconcile()
        generation = harness.current_generation
        assert generation is not None
        return await harness.tool_executor.execute(
            ToolRequest(
                name="echo",
                args={"text": "hi"},
                generation_id=generation_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
            ),
            snapshot=harness.tool_snapshot(generation),
        )


async def test_denial_after_recording_applies_to_replayed_calls() -> None:
    recording = session(ReplayMode.RECORD)
    await record_one_call(recording)
    recording.mode = ReplayMode.REPLAY

    from chassis.policy import DenyAllPolicy

    async with TestHarness(replay=recording) as harness:
        harness.install_tools(
            fake_tool("echo", result="live", parameters={"text": (str, ...)}),
            policies={"echo": ToolPolicy(permissions=("network.fetch",))},
        )
        await harness.reconcile()
        generation = harness.current_generation
        assert generation is not None

        with pytest.raises(PolicyDenied):
            await harness.tool_executor.execute(
                ToolRequest(name="echo", args={"text": "hi"}),
                snapshot=harness.tool_snapshot(generation),
                policy=DenyAllPolicy(),
            )


async def test_replayed_calls_fire_the_after_hook_and_span() -> None:
    from chassis.hooks import HookEvent
    from chassis.telemetry import RecordingTelemetry

    recording = session(ReplayMode.RECORD)
    await record_one_call(recording)
    recording.mode = ReplayMode.REPLAY

    seen: list[str] = []
    telemetry = RecordingTelemetry()

    async with TestHarness(replay=recording, telemetry=telemetry) as harness:
        harness.install_tools(fake_tool("echo", result="live", parameters={"text": (str, ...)}))

        async def watcher(payload: Mapping[str, Any]) -> None:
            seen.append(str(payload.get("status")))

        @plugin(name="watch", version="1.0.0")
        async def watch(ctx: PluginContext) -> None:
            ctx.hooks.register(HookEvent.AFTER_TOOL_EXECUTE, watcher)

        harness.install(watch, entry_id="watch")
        await harness.reconcile()
        generation = harness.current_generation
        assert generation is not None

        result = await harness.tool_executor.execute(
            ToolRequest(name="echo", args={"text": "hi"}),
            snapshot=harness.tool_snapshot(generation),
        )

        assert result.content == "echoed"  # the recorded semantic result
        assert seen == ["ok"]  # the live after-hook observed it

        span = telemetry.spans_named("tool.execute")[0]
        assert span.attributes["replayed"] is True
        assert span.attributes["tool"] == "echo"


async def test_replay_stamps_current_attribution() -> None:
    recording = session(ReplayMode.RECORD)
    recorded = await record_one_call(recording)
    recording.mode = ReplayMode.REPLAY

    calls: list[Any] = []
    async with TestHarness(replay=recording) as harness:
        harness.install_tools(
            fake_tool("echo", result="live", parameters={"text": (str, ...)}, calls=calls)
        )
        await harness.reconcile()
        generation = harness.current_generation
        assert generation is not None

        result = await harness.tool_executor.execute(
            ToolRequest(
                name="echo",
                args={"text": "hi"},
                generation_id="gen-new",
                run_id="run-new",
                tool_call_id="call-new",
            ),
            snapshot=harness.tool_snapshot(generation),
        )

        # The semantic result is historical; the attribution is current.
        assert result.content == "echoed"
        assert calls == []  # the live tool never ran
        assert result.generation_id == "gen-new"
        assert result.run_id == "run-new"
        assert result.tool_call_id == "call-new"
        assert result.duration_seconds >= 0.0
        assert recorded.generation_id == "gen-old"
        assert recorded.run_id == "run-old"


def test_replay_exhaustion_is_distinguishable_from_a_missing_key() -> None:
    recording = session(ReplayMode.RECORD)
    recording.record(BoundaryKind.TOOL, key="known")

    recording.mode = ReplayMode.REPLAY
    first = recording.replay(BoundaryKind.TOOL, key="known")
    assert first.key == "known"

    with pytest.raises(ReplayMismatch) as exhausted:
        recording.replay(BoundaryKind.TOOL, key="known")
    assert exhausted.value.context["reason"] == "exhausted"

    with pytest.raises(ReplayMismatch) as missing:
        recording.replay(BoundaryKind.TOOL, key="never")
    assert missing.value.context["reason"] == "missing"
