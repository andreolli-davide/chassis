"""Bounded record/replay of harness-controlled boundaries.

Replay covers what Chassis mediates: tool calls, wrapped model calls, interrupt
values, runtime snapshots, and selected lifecycle events. It does not virtualize
clocks, randomness, HTTP, databases, or other external systems, and it never
claims to; an unrecorded operation fails loudly or runs live according to an
explicit policy.
"""

from __future__ import annotations

from chassis.replay.model import ReplayChatModel
from chassis.replay.models import (
    BoundaryKind,
    ReplayFallback,
    ReplayMode,
    ReplayRecord,
)
from chassis.replay.session import ReplaySession, boundary_key

__all__ = [
    "BoundaryKind",
    "ReplayChatModel",
    "ReplayFallback",
    "ReplayMode",
    "ReplayRecord",
    "ReplaySession",
    "boundary_key",
]
