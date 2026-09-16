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
        return cls(
            kind=BoundaryKind(payload["kind"]),
            sequence=int(payload["sequence"]),
            key=str(payload["key"]),
            request=dict(payload.get("request") or {}),
            response=payload.get("response"),
            generation_id=payload.get("generation_id"),
            run_id=payload.get("run_id"),
            created_at=float(payload.get("created_at", time.time())),
        )
