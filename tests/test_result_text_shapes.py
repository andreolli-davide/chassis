from __future__ import annotations

from typing import Any

from chassis.runtime import AgentResult


class Msg:
    """Attribute-based message: carries its content as an attribute."""

    def __init__(self, content: Any) -> None:
        self.content = content


def result_with(*messages: Any) -> AgentResult:
    return AgentResult(
        agent="a",
        generation_id="g",
        run_id="r",
        output={"messages": list(messages)},
    )


def test_mapping_message_with_string_content() -> None:
    # A plain mapping message, as produced by LangGraph state.
    assert result_with({"content": "hello"}).text == "hello"


def test_mapping_message_with_mapping_content() -> None:
    assert result_with({"content": {"text": "mapped"}}).text == "mapped"


def test_mapping_message_with_text_blocks() -> None:
    assert (
        result_with(
            {"content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]}
        ).text
        == "one\ntwo"
    )


def test_mapping_messages_match_their_attribute_based_shapes() -> None:
    shapes = (
        "plain",
        {"text": "mapped"},
        [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}],
    )
    for content in shapes:
        assert result_with({"content": content}).text == result_with(Msg(content)).text


def test_mixed_message_shapes_read_like_their_attribute_based_equivalent() -> None:
    mixed = result_with({"content": "first"}, Msg({"text": "second"}))
    attribute_only = result_with(Msg("first"), Msg({"text": "second"}))
    assert mixed.text == attribute_only.text == "second"


def test_non_textual_mapping_messages_contribute_nothing() -> None:
    assert result_with({"content": "kept"}, {"content": {"image": "chart.png"}}).text == "kept"
    assert result_with({"content": {"image": "chart.png"}}).text is None
