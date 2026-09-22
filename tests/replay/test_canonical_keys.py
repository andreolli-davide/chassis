from __future__ import annotations

from typing import Any

import pytest

from chassis.core.errors import ConfigurationError
from chassis.replay import ReplayChatModel, ReplayMode, ReplaySession
from chassis.testing import FakeChatModel


def session(mode: ReplayMode, **kwargs: Any) -> ReplaySession:
    return ReplaySession(mode=mode, **kwargs)


def messages(text: str = "hi") -> list[Any]:
    from langchain_core.messages import HumanMessage

    return [HumanMessage(content=text)]


def test_non_string_mapping_keys_are_rejected_not_stringified() -> None:
    """``{1: ..., "1": ...}`` must never collapse onto one replay key."""

    recording = session(ReplayMode.RECORD)
    model = ReplayChatModel(session=recording, inner=FakeChatModel(responses=["x"]), model_name="m")

    with pytest.raises(ConfigurationError):
        model._generate(messages(), provider_options={1: "int", "1": "str"})  # type: ignore[call-arg]
    with pytest.raises(ConfigurationError):
        model._generate(messages(), provider_options={2: "int-only"})  # type: ignore[call-arg]
    assert recording.records == []


def test_nested_non_string_mapping_keys_are_rejected() -> None:
    recording = session(ReplayMode.RECORD)
    model = ReplayChatModel(session=recording, inner=FakeChatModel(responses=["x"]), model_name="m")

    with pytest.raises(ConfigurationError):
        model._generate(messages(), provider_options={"outer": {1: "int", "1": "str"}})  # type: ignore[call-arg]
    with pytest.raises(ConfigurationError):
        model._generate(messages(), provider_options=[{"inner": [{2: "deep"}]}])  # type: ignore[call-arg]
    assert recording.records == []


def test_string_mapping_keys_still_record_and_replay() -> None:
    recording = session(ReplayMode.RECORD)
    model = ReplayChatModel(
        session=recording, inner=FakeChatModel(responses=["answer"]), model_name="m"
    )

    model._generate(messages(), provider_options={"1": "str", "outer": {"inner": [1, 2.5]}})  # type: ignore[call-arg]

    replaying = ReplaySession(mode=ReplayMode.REPLAY, records=list(recording.records))
    replay_model = ReplayChatModel(session=replaying, inner=None, model_name="m")

    # Mapping key order is not semantic: the replay key must still match.
    result = replay_model._generate(  # type: ignore[call-arg]
        messages(), provider_options={"outer": {"inner": [1, 2.5]}, "1": "str"}
    )
    assert result.generations[0].message.content == "answer"
