"""Recording and replaying harness-controlled boundaries.

A session owns one mode and one record list. In ``record`` mode every boundary is
appended; in ``replay`` mode a matching record answers instead of performing the
operation; in ``live`` mode the session is inert.

Matching is by canonical key *and* kind. A recording that does not correspond to
the operation being replayed produces :class:`~chassis.core.errors.ReplayMismatch`
rather than a wrong answer -- the failure mode the specification calls out
explicitly.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from chassis.core.errors import ReplayMismatch
from chassis.persistence.hashing import stable_hash
from chassis.replay.models import (
    BoundaryKind,
    ReplayFallback,
    ReplayMode,
    ReplayRecord,
)
from chassis.secrets.redaction import SecretRedactor

__all__ = ["ReplaySession", "boundary_key"]

T = TypeVar("T")

#: Key fragments that are treated as secret when a boundary is recorded.
_SENSITIVE_REQUEST_KEYS = ("api_key", "authorization", "token", "password", "secret")


def boundary_key(*parts: Any) -> str:
    """Canonical identity of a boundary interaction."""

    return stable_hash(list(parts), length=32)


@dataclass(slots=True)
class ReplaySession:
    """Bounded record/replay of harness-controlled boundaries.

    Args:
        mode: ``live``, ``record``, or ``replay``.
        fallback: What replay does when an operation was never recorded.
        redactor: Redactor applied to recorded payloads, so a recording never
            becomes a place where secrets accumulate.
        metadata: Session-level metadata (dataset name, harness version, ...).
    """

    mode: ReplayMode = ReplayMode.LIVE
    fallback: ReplayFallback = ReplayFallback.ERROR
    redactor: SecretRedactor = field(default_factory=SecretRedactor)
    metadata: dict[str, Any] = field(default_factory=dict)
    records: list[ReplayRecord] = field(default_factory=list)
    _cursor: dict[str, int] = field(default_factory=dict)

    @property
    def is_recording(self) -> bool:
        return self.mode is ReplayMode.RECORD

    @property
    def is_replaying(self) -> bool:
        return self.mode is ReplayMode.REPLAY

    # ------------------------------------------------------------------ writing

    def record(
        self,
        kind: BoundaryKind,
        *,
        key: str,
        request: Mapping[str, Any] | None = None,
        response: Any = None,
        generation_id: str | None = None,
        run_id: str | None = None,
    ) -> ReplayRecord | None:
        """Append a boundary interaction when recording. Otherwise a no-op."""

        if not self.is_recording:
            return None
        record = ReplayRecord(
            kind=kind,
            sequence=len(self.records),
            key=key,
            request=self.redactor.redact_value(_redact_sensitive(dict(request or {}))),
            response=self.redactor.redact_value(response),
            generation_id=generation_id,
            run_id=run_id,
        )
        self.records.append(record)
        return record

    def record_lifecycle(self, event: str, payload: Mapping[str, Any] | None = None) -> None:
        """Record a selected lifecycle event."""

        self.record(
            BoundaryKind.LIFECYCLE,
            key=boundary_key("lifecycle", event),
            request={"event": event, **dict(payload or {})},
        )

    def record_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        """Record a runtime snapshot, so a recording explains its own composition."""

        self.record(
            BoundaryKind.SNAPSHOT,
            key=boundary_key("snapshot", snapshot.get("generation_id")),
            request={},
            response=dict(snapshot),
        )

    # ------------------------------------------------------------------ reading

    def replay(self, kind: BoundaryKind, *, key: str) -> ReplayRecord:
        """Return the recorded interaction for ``kind`` and ``key``.

        Raises:
            ReplayMismatch: nothing was recorded for this boundary.
        """

        consumed = self._cursor.get(kind.value, 0)
        for index, record in enumerate(self.records):
            if record.kind is not kind or record.key != key:
                continue
            if index < consumed:
                continue
            self._cursor[kind.value] = index + 1
            return record
        raise ReplayMismatch(
            f"no recorded {kind.value} interaction matches this operation",
            kind=kind.value,
            key=key,
            recorded=len([record for record in self.records if record.kind is kind]),
        )

    def has(self, kind: BoundaryKind, *, key: str) -> bool:
        return any(record.kind is kind and record.key == key for record in self.records)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records:
            counts[record.kind.value] = counts.get(record.kind.value, 0) + 1
        return dict(sorted(counts.items()))

    # --------------------------------------------------------------- execution

    # ------------------------------------------------------------------ storage

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "fallback": self.fallback.value,
            "metadata": dict(self.metadata),
            "records": [record.to_dict() for record in self.records],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], **overrides: Any) -> ReplaySession:
        arguments: dict[str, Any] = {
            "mode": ReplayMode(payload.get("mode", ReplayMode.LIVE)),
            "fallback": ReplayFallback(payload.get("fallback", ReplayFallback.ERROR)),
            "metadata": dict(payload.get("metadata") or {}),
        }
        arguments.update(overrides)
        session = cls(**arguments)
        session.records = [
            ReplayRecord.from_dict(dict(record)) for record in payload.get("records", [])
        ]
        return session

    def save(self, path: str | Path) -> Path:
        """Write the recording as JSON."""

        target = Path(path)
        target.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path, **overrides: Any) -> ReplaySession:
        """Read a recording written by :meth:`save`."""

        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(payload, **overrides)


def _redact_sensitive(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Drop obviously sensitive request fields before a payload is recorded."""

    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        if any(fragment in str(key).lower() for fragment in _SENSITIVE_REQUEST_KEYS):
            redacted[str(key)] = "<redacted>"
        else:
            redacted[str(key)] = value
    return redacted
