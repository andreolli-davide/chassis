"""Persistence concerns owned by Chassis: canonical hashing, runtime snapshots,
and explicit format versioning for persisted documents.

LangGraph checkpointers own durable graph/thread execution state and LangGraph
Store owns cross-thread agent data; Chassis never mirrors either into a second
checkpoint system. Documents Chassis does persist — snapshot records and replay
recordings — declare their own format version and migrate or reject explicitly
(:mod:`chassis.persistence.formats`, ``docs/compatibility.md``).
"""

from __future__ import annotations

from chassis.persistence.formats import (
    DIAGNOSTICS_FORMAT_VERSION,
    PLAN_FORMAT_VERSION,
    REPLAY_FORMAT_VERSION,
    SNAPSHOT_FORMAT_VERSION,
    declared_format_version,
    migrate_payload,
)
from chassis.persistence.hashing import (
    canonical_json,
    hash_text,
    prompt_hash,
    schema_hash,
    stable_hash,
    tool_schema_hash,
    tool_schema_payload,
)
from chassis.persistence.snapshots import RuntimeSnapshot, chassis_version

__all__ = [
    "DIAGNOSTICS_FORMAT_VERSION",
    "PLAN_FORMAT_VERSION",
    "REPLAY_FORMAT_VERSION",
    "SNAPSHOT_FORMAT_VERSION",
    "RuntimeSnapshot",
    "canonical_json",
    "chassis_version",
    "declared_format_version",
    "hash_text",
    "migrate_payload",
    "prompt_hash",
    "schema_hash",
    "stable_hash",
    "tool_schema_hash",
    "tool_schema_payload",
]
