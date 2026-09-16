from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chassis import MODEL
from chassis.core.errors import ReplayMismatch
from chassis.replay import (
    BoundaryKind,
    ReplayChatModel,
    ReplayFallback,
    ReplayMode,
    ReplaySession,
    boundary_key,
)
from chassis.testing import FakeChatModel, TestHarness, fake_tool
from chassis.tools import ToolRequest

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
    assert recording.counts() == {"tool": 1}

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


async def test_through_replays_recorded_operations_and_records_new_ones() -> None:
    recording = session(ReplayMode.RECORD)
    calls: list[int] = []

    async def operation() -> dict[str, int]:
        calls.append(1)
        return {"value": 42}

    first = await recording.through(BoundaryKind.MODEL, key="k", operation=operation)
    assert first == {"value": 42}
    assert calls == [1]

    replaying = ReplaySession(mode=ReplayMode.REPLAY, records=list(recording.records))
    second = await replaying.through(BoundaryKind.MODEL, key="k", operation=operation)
    assert second == {"value": 42}
    assert calls == [1]


async def test_snapshot_and_lifecycle_records_capture_composition() -> None:
    recording = session(ReplayMode.RECORD)
    generation_id = ""
    async with TestHarness() as harness:
        harness.provide(MODEL, "model")
        await harness.start()
        generation = harness.current_generation
        assert generation is not None
        generation_id = generation.generation_id

        recording.record_snapshot(harness.snapshot_for(generation).to_dict())
        recording.record_lifecycle(
            "generation.publish", {"generation_id": generation.generation_id}
        )

    payload = recording.records[0].response
    assert payload["generation_id"] == generation_id
    assert "plugins" in payload
    assert recording.records[1].request["event"] == "generation.publish"


def test_session_counts_group_by_boundary_kind() -> None:
    recording = session(ReplayMode.RECORD)
    recording.record(BoundaryKind.TOOL, key="a")
    recording.record(BoundaryKind.TOOL, key="b")
    recording.record(BoundaryKind.MODEL, key="c")

    assert recording.counts() == {"model": 1, "tool": 2}
