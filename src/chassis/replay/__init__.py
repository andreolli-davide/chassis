"""Bounded record/replay of harness-controlled boundaries.

Replay covers what Chassis mediates: tool calls, wrapped model calls, interrupt
values, runtime snapshots, and selected lifecycle events. It does not virtualize
clocks, randomness, HTTP, databases, or other external systems, and it never
claims to; an unrecorded operation fails loudly or runs live according to an
explicit policy.

The session and record types import nothing beyond the core. The replayable chat
model is a ``langchain-core`` object, so it is imported lazily: touching
:class:`ReplayChatModel` loads it, and the ``langgraph`` extra is required for it.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from chassis.replay.models import (
    BoundaryKind,
    ReplayFallback,
    ReplayMode,
    ReplayRecord,
)
from chassis.replay.session import ReplaySession, boundary_key

if TYPE_CHECKING:
    from chassis.replay.model import ReplayChatModel

__all__ = [
    "BoundaryKind",
    "ReplayChatModel",
    "ReplayFallback",
    "ReplayMode",
    "ReplayRecord",
    "ReplaySession",
    "boundary_key",
]

_LAZY = {"ReplayChatModel": "chassis.replay.model"}


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module_name), name)
