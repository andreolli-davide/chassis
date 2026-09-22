from __future__ import annotations

from typing import Any

import pytest

from chassis.core.errors import ReplayMismatch
from chassis.replay import ReplayChatModel, ReplayMode, ReplaySession
from chassis.testing import FakeChatModel, TestHarness, fake_tool
from chassis.tools import ToolRequest


def session(mode: ReplayMode, **kwargs: Any) -> ReplaySession:
    return ReplaySession(mode=mode, **kwargs)


def messages(text: str = "hi") -> list[Any]:
    from langchain_core.messages import HumanMessage

    return [HumanMessage(content=text)]


async def record_tool_call(recording: ReplaySession) -> None:
    async with TestHarness(replay=recording) as harness:
        harness.install_tools(fake_tool("echo", result="echoed", parameters={"text": (str, ...)}))
        await harness.reconcile()
        generation = harness.current_generation
        assert generation is not None
        await harness.tool_executor.execute(
            ToolRequest(name="echo", args={"text": "hi"}),
            snapshot=harness.tool_snapshot(generation),
        )


async def run_tool_call(replaying: ReplaySession, text: str) -> Any:
    async with TestHarness(replay=replaying) as harness:
        harness.install_tools(
            fake_tool("echo", result="SHOULD NOT RUN", parameters={"text": (str, ...)})
        )
        await harness.reconcile()
        generation = harness.current_generation
        assert generation is not None
        return await harness.tool_executor.execute(
            ToolRequest(name="echo", args={"text": text}),
            snapshot=harness.tool_snapshot(generation),
        )


async def test_tool_boundary_reports_exhausted_reason() -> None:
    recording = session(ReplayMode.RECORD)
    await record_tool_call(recording)

    replaying = ReplaySession(mode=ReplayMode.REPLAY, records=list(recording.records))
    first = await run_tool_call(replaying, "hi")
    assert first.content == "echoed"

    with pytest.raises(ReplayMismatch) as exhausted:
        await run_tool_call(replaying, "hi")

    assert exhausted.value.context["reason"] == "exhausted"


async def test_tool_boundary_reports_missing_reason() -> None:
    recording = session(ReplayMode.RECORD)
    await record_tool_call(recording)

    replaying = ReplaySession(mode=ReplayMode.REPLAY, records=list(recording.records))
    with pytest.raises(ReplayMismatch) as missing:
        await run_tool_call(replaying, "other")

    assert missing.value.context["reason"] == "missing"


def test_model_boundary_reports_exhausted_reason() -> None:
    recording = session(ReplayMode.RECORD)
    model = ReplayChatModel(
        session=recording, inner=FakeChatModel(responses=["answer"]), model_name="m"
    )
    model._generate(messages())  # type: ignore[call-arg]

    replaying = ReplaySession(mode=ReplayMode.REPLAY, records=list(recording.records))
    replay_model = ReplayChatModel(session=replaying, inner=None, model_name="m")
    replay_model._generate(messages())  # type: ignore[call-arg]

    with pytest.raises(ReplayMismatch) as exhausted:
        replay_model._generate(messages())  # type: ignore[call-arg]

    assert exhausted.value.context["reason"] == "exhausted"


def test_model_boundary_reports_missing_reason() -> None:
    recording = session(ReplayMode.RECORD)
    model = ReplayChatModel(
        session=recording, inner=FakeChatModel(responses=["answer"]), model_name="m"
    )
    model._generate(messages())  # type: ignore[call-arg]

    replaying = ReplaySession(mode=ReplayMode.REPLAY, records=list(recording.records))
    replay_model = ReplayChatModel(session=replaying, inner=None, model_name="m")

    with pytest.raises(ReplayMismatch) as missing:
        replay_model._generate(messages("different"))  # type: ignore[call-arg]

    assert missing.value.context["reason"] == "missing"
