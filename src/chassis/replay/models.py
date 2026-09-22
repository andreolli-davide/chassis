"""Record/replay data model.

Replay in Chassis is deliberately bounded. It records the boundaries Chassis
actually controls:

* tool requests and results;
* model requests and responses, when the model is wrapped;
* interrupt values;
* runtime snapshots;
* selected lifecycle events.

It does **not** virtualize clocks, randomness, HTTP, databases, or any other
external system, and it makes no claim to. An operation that was never recorded is
answered according to an explicit policy -- fail, or run live -- never by guessing.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "BoundaryKind",
    "ReplayFallback",
    "ReplayMode",
    "ReplayRecord",
]


class ReplayMode(StrEnum):
    """How a session treats boundaries."""

    LIVE = "live"
    RECORD = "record"
    REPLAY = "replay"


class BoundaryKind(StrEnum):
    """An explicit boundary Chassis controls."""

    TOOL = "tool"
    MODEL = "model"
    INTERRUPT = "interrupt"
    SNAPSHOT = "snapshot"
    LIFECYCLE = "lifecycle"


class ReplayFallback(StrEnum):
    """What to do when replay meets an operation that was not recorded."""

    ERROR = "error"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class ReplayRecord:
    """One recorded boundary interaction.

    ``key`` is the identity of the boundary: for a tool call it covers the tool
    name and canonical arguments, for a model call the model identity and the
    request. Replay compares keys so that a recording cannot silently answer the
    wrong request.
    """

    kind: BoundaryKind
    sequence: int
    key: str
    request: dict[str, Any] = field(default_factory=dict)
    response: Any = None
    generation_id: str | None = None
    run_id: str | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "sequence": self.sequence,
            "key": self.key,
            "request": dict(self.request),
            "response": self.response,
            "generation_id": self.generation_id,
            "run_id": self.run_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ReplayRecord:
        """Rebuild one record, validating shape instead of coercing it.

        Malformed fields raise ``TypeError``/``ValueError`` (surfaced as a
        corrupted document by the session reader) — a wrong-typed field is
        never stringified or defaulted into a plausible record.

        Raises:
            KeyError: a required field is missing.
            TypeError: a field has the wrong type.
            ValueError: ``kind`` is not a known boundary.
        """

        sequence = payload["sequence"]
        key = payload["key"]
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise TypeError("record sequence is not an integer")
        if not isinstance(key, str):
            raise TypeError("record key is not a string")
        request = payload.get("request")
        if request is not None and not isinstance(request, Mapping):
            raise TypeError("record request is not a mapping")
        generation_id = payload.get("generation_id")
        if generation_id is not None and not isinstance(generation_id, str):
            raise TypeError("record generation_id is not a string")
        run_id = payload.get("run_id")
        if run_id is not None and not isinstance(run_id, str):
            raise TypeError("record run_id is not a string")
        created_at = payload.get("created_at", 0.0)
        if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
            raise TypeError("record created_at is not a number")
        return cls(
            kind=BoundaryKind(payload["kind"]),
            sequence=sequence,
            key=key,
            request=dict(request or {}),
            response=payload.get("response"),
            generation_id=generation_id,
            run_id=run_id,
            created_at=float(created_at),
        )
