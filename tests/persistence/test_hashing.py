from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

import pytest
from langchain_core.tools import tool
from pydantic import BaseModel
from typing_extensions import TypedDict

from chassis.core.errors import ConfigurationError
from chassis.persistence import (
    canonical_json,
    hash_text,
    prompt_hash,
    schema_hash,
    stable_hash,
    tool_schema_hash,
    tool_schema_payload,
)


def test_canonical_json_sorts_keys_and_normalizes_sets() -> None:
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert canonical_json({"a", "b"}) == canonical_json({"b", "a"})
    assert canonical_json([1, 2, 3]) == "[1,2,3]"
    assert canonical_json("è") == '"è"'


def test_canonical_json_normalizes_floats_and_enums() -> None:
    from enum import StrEnum

    class Colour(StrEnum):
        RED = "red"

    assert canonical_json({"v": 1.0}) == canonical_json({"v": 1})
    assert canonical_json(Colour.RED) == '"red"'


def test_canonical_json_serializes_pydantic_and_dataclasses() -> None:
    class Model(BaseModel):
        b: int
        a: int

    @dataclass
    class Record:
        b: int
        a: int

    assert canonical_json(Model(b=1, a=2)) == canonical_json(Record(b=1, a=2))


def test_arbitrary_objects_are_rejected_rather_than_stringified() -> None:
    class Opaque:
        pass

    with pytest.raises(ConfigurationError) as excinfo:
        canonical_json({"value": Opaque()})

    assert excinfo.value.context["value_type"] == "Opaque"


def test_stable_hash_is_deterministic_and_ordered() -> None:
    first = stable_hash({"a": 1, "b": [1, 2]})
    second = stable_hash({"b": [1, 2], "a": 1})

    assert first == second
    assert len(first) == 64
    assert len(stable_hash({"a": 1}, length=8)) == 8
    assert stable_hash({"a": 1}) != stable_hash({"a": 2})


def test_hash_text_matches_the_documented_algorithm() -> None:
    import hashlib

    assert hash_text("chassis") == hashlib.sha256(b"chassis").hexdigest()


def test_schema_hash_handles_typed_dicts_and_pydantic_models() -> None:
    class State(TypedDict):
        messages: Annotated[list[str], "reducer"]
        count: int

    class Model(BaseModel):
        messages: list[str]

    typed_dict_hash = schema_hash(State)
    assert typed_dict_hash == schema_hash(State)
    assert typed_dict_hash != schema_hash(Model)
    assert schema_hash(Model) == schema_hash(Model)


def test_schema_hash_rejects_unhashable_schemas() -> None:
    with pytest.raises(ConfigurationError):
        schema_hash(object())


@tool
def add_numbers(a: int, b: int) -> int:
    """Add two integers."""

    return a + b


@tool
def concatenate(text: str) -> str:
    """Join text."""

    return text


def test_tool_schema_payload_describes_the_external_contract() -> None:
    payload = tool_schema_payload(add_numbers)

    assert payload["name"] == "add_numbers"
    assert payload["description"] == "Add two integers."
    assert "a" in payload["args"]["properties"]


def test_tool_schema_hash_ignores_implementation_and_order() -> None:
    from langchain_core.tools import StructuredTool

    async def different_implementation(**kwargs: Any) -> int:
        return 0

    same_contract = StructuredTool(
        name=add_numbers.name,
        description=add_numbers.description,
        args_schema=add_numbers.get_input_schema(),
        coroutine=different_implementation,
    )

    # A different callable with the same contract must not invalidate a graph.
    assert tool_schema_hash([add_numbers]) == tool_schema_hash([same_contract])
    assert tool_schema_hash([add_numbers, concatenate]) == tool_schema_hash(
        [concatenate, add_numbers]
    )
    assert tool_schema_hash([add_numbers]) != tool_schema_hash([concatenate])


def test_tool_schema_hash_changes_when_the_contract_changes() -> None:
    from langchain_core.tools import StructuredTool
    from pydantic import create_model

    schema = create_model("Args", text=(str, ...), count=(int, ...))

    async def _run(**kwargs: Any) -> str:
        return "ok"

    wider = StructuredTool(
        name=add_numbers.name,
        description=add_numbers.description,
        args_schema=schema,
        coroutine=_run,
    )

    assert tool_schema_hash([add_numbers]) != tool_schema_hash([wider])


def test_tool_schema_hash_surfaces_unserializable_schemas() -> None:
    class NotATool:
        name = "broken"
        description = "broken"

        def get_input_schema(self) -> Any:
            raise RuntimeError("no schema")

    with pytest.raises(ConfigurationError) as excinfo:
        tool_schema_hash([NotATool()])

    assert excinfo.value.context["tool"] == "broken"


def test_prompt_hash_normalizes_surrounding_whitespace() -> None:
    assert prompt_hash("  hello  ") == prompt_hash("hello")
    assert prompt_hash("hello") != prompt_hash("hello world")


# --------------------------------------------------------------------------
# Canonicalization (R021): string keys, deterministic primitives.
# --------------------------------------------------------------------------


def test_canonical_hashing_requires_string_mapping_keys() -> None:
    with pytest.raises(ConfigurationError):
        stable_hash({"a": 1, 2: "b"})
    with pytest.raises(ConfigurationError):
        stable_hash({"nested": {1: "x"}})
    with pytest.raises(ConfigurationError):
        stable_hash({True: "y"})

    # String keys never collide after normalization.
    assert stable_hash({"1": "a"}) != stable_hash({"1": "b"})


def test_unicode_is_hashed_without_silent_normalization() -> None:
    composed = "cafe\u0301"
    single = "caf\u00e9"

    assert stable_hash(single) == stable_hash(single)
    assert stable_hash(composed) != stable_hash(single)


def test_signed_zero_and_integral_floats_normalize() -> None:
    assert stable_hash({"x": 0.0}) == stable_hash({"x": -0.0}) == stable_hash({"x": 0})
    assert stable_hash({"x": 1.0}) == stable_hash({"x": 1})


def test_non_finite_floats_are_deterministic() -> None:
    assert stable_hash({"x": float("nan")}) == stable_hash({"x": float("nan")})
    assert stable_hash({"x": float("inf")}) == stable_hash({"x": float("inf")})
    assert stable_hash({"x": float("nan")}) != stable_hash({"x": float("inf")})


def test_decimal_like_values_are_rejected_not_guessed() -> None:
    from decimal import Decimal

    with pytest.raises(ConfigurationError):
        stable_hash({"x": Decimal("1.5")})
    assert stable_hash({"x": "1.5"}) != stable_hash({"x": 1.5})


def test_nested_order_variation_hashes_identically() -> None:
    first = {"a": {"x": 1, "y": 2}, "b": [1, 2]}
    second = {"b": [1, 2], "a": {"y": 2, "x": 1}}

    assert stable_hash(first) == stable_hash(second)
