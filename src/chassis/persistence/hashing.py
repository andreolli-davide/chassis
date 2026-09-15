"""Canonical serialization and stable hashes.

Hashes identify *versions of things* -- a state schema, a tool set, a plugin
graph, a runtime snapshot -- so the rules must be documented and stable:

1.  the payload is reduced to JSON-compatible data;
2.  mapping keys are sorted;
3.  sequences keep their order, sets are sorted;
4.  serialization uses UTF-8, no ASCII escaping, and compact separators;
5.  the digest is SHA-256 unless a caller asks otherwise.

Values whose representation is not stable (arbitrary objects) are rejected rather
than stringified: a hash built from ``repr`` or object identity would silently
produce different digests for the same logical state.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from chassis.core.errors import ConfigurationError

__all__ = [
    "canonical_json",
    "hash_text",
    "prompt_hash",
    "schema_hash",
    "stable_hash",
    "tool_schema_hash",
    "tool_schema_payload",
]

_DEFAULT_ALGORITHM = "sha256"


@runtime_checkable
class ToolLike(Protocol):
    """Structural view of a tool needed for schema hashing.

    Duck-typed on purpose so the hashing module does not depend on a tool library.
    """

    name: str
    description: str

    def get_input_schema(self) -> Any:
        """Return the tool's input schema (a pydantic model)."""

        ...


def _canonicalize(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # Normalize integral floats so 1.0 and 1 cannot produce different digests.
        if value.is_integer() and abs(value) < 2**53:
            return int(value)
        return round(value, 12)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(item) for key, item in sorted(value.items(), key=_key)}
    if isinstance(value, (set, frozenset)):
        return sorted((_canonicalize(item) for item in value), key=_order)
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _canonicalize(model_dump(mode="json"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonicalize(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    raise ConfigurationError(
        "value cannot be canonically serialized", value_type=type(value).__name__
    )


def _key(item: tuple[Any, Any]) -> str:
    return str(item[0])


def _order(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def canonical_json(payload: Any) -> str:
    """Serialize ``payload`` canonically."""

    return json.dumps(
        _canonicalize(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def hash_text(text: str, *, algorithm: str = _DEFAULT_ALGORITHM) -> str:
    """Hash a string with a documented algorithm."""

    return hashlib.new(algorithm, text.encode("utf-8")).hexdigest()


def stable_hash(
    payload: Any, *, algorithm: str = _DEFAULT_ALGORITHM, length: int | None = None
) -> str:
    """Hash a payload through canonical serialization."""

    digest = hash_text(canonical_json(payload), algorithm=algorithm)
    return digest if length is None else digest[:length]


def schema_hash(schema: Any) -> str:
    """Hash a state schema (pydantic model, TypedDict, or mapping of annotations)."""

    model_json_schema = getattr(schema, "model_json_schema", None)
    if callable(model_json_schema):
        return stable_hash(model_json_schema())
    annotations = getattr(schema, "__annotations__", None)
    if isinstance(annotations, Mapping):
        return stable_hash(
            {
                "schema": getattr(schema, "__name__", "anonymous"),
                "fields": {name: str(annotation) for name, annotation in annotations.items()},
            }
        )
    if isinstance(schema, Mapping):
        return stable_hash({name: str(annotation) for name, annotation in schema.items()})
    raise ConfigurationError("state schema cannot be hashed", schema=type(schema).__name__)


def tool_schema_payload(tool: ToolLike) -> dict[str, Any]:
    """Canonical description of one tool's externally visible contract."""

    try:
        input_schema = tool.get_input_schema().model_json_schema()
    except Exception as error:
        raise ConfigurationError(
            "tool input schema cannot be serialized", tool=tool.name
        ) from error
    return {
        "name": tool.name,
        "description": tool.description,
        "args": input_schema,
    }


def tool_schema_hash(tools: Iterable[ToolLike]) -> str:
    """Hash the tool contracts a graph is compiled against.

    Only externally visible structure is hashed: name, description, and argument
    schema. Implementation changes that keep the contract identical must not
    invalidate a compiled graph.
    """

    payload = [tool_schema_payload(tool) for tool in tools]
    payload.sort(key=lambda item: item["name"])
    return stable_hash(payload)


def prompt_hash(text: str) -> str:
    """Hash a prompt body for snapshot attribution."""

    return hash_text(text.strip())
