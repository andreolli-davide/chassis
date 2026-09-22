from __future__ import annotations

from typing import Any

import pytest
from langchain_core.tools import tool

from chassis.core.errors import ToolExecutionError
from chassis.core.scope import Scope
from chassis.replay import ReplayMode, ReplaySession
from chassis.tools import ToolExecutor, ToolRegistry, ToolRequest


@tool
def explode(reason: str) -> str:
    """Always fail."""

    raise RuntimeError("boom")


def build_snapshot(*tools: Any):  # type: ignore[no-untyped-def]
    registry = ToolRegistry()
    scope = Scope("tools")
    for entry in tools:
        registry.register(scope=scope, tool=entry, policy=None, owner_name="test-owner")
    return registry.snapshot("gen_0001")


async def test_replayed_error_matches_live_under_raise_on_error() -> None:
    """R016 parity: a recorded failure behaves exactly like a live one."""

    snapshot = build_snapshot(explode)
    request = ToolRequest(name="explode", args={"reason": "x"})

    # The live failure, normalized and recorded (raise_on_error=False returns).
    recording = ReplaySession(mode=ReplayMode.RECORD)
    live_returned = await ToolExecutor(replay=recording).execute(request, snapshot=snapshot)
    assert live_returned.status == "error"
    assert live_returned.error is not None

    # The same live call under raise_on_error=True raises ...
    with pytest.raises(ToolExecutionError) as live:
        await ToolExecutor().execute(request, snapshot=snapshot, raise_on_error=True)

    # ... and so must the replayed copy of the same recorded error result.
    replaying = ReplaySession(mode=ReplayMode.REPLAY, records=list(recording.records))
    with pytest.raises(ToolExecutionError) as replayed:
        await ToolExecutor(replay=replaying).execute(
            request, snapshot=snapshot, raise_on_error=True
        )

    assert replayed.value.message == live.value.message
    assert replayed.value.context == live.value.context

    # Under raise_on_error=False both return the same normalized failure.
    replaying_again = ReplaySession(mode=ReplayMode.REPLAY, records=list(recording.records))
    replayed_result = await ToolExecutor(replay=replaying_again).execute(request, snapshot=snapshot)

    assert replayed_result.status == "error"
    assert replayed_result.error == live_returned.error
    assert replayed_result.content is None
