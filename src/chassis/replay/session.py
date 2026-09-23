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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeVar

from chassis.core.errors import FormatError, ReplayMismatch
from chassis.persistence.formats import REPLAY_FORMAT_VERSION, migrate_payload
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
            request=self.redactor.redact_value(dict(request or {})),
            response=self.redactor.redact_value(response),
            generation_id=generation_id,
            run_id=run_id,
        )
        self.records.append(record)
        return record

    def record_fallback(
        self,
        kind: BoundaryKind,
        *,
        key: str,
        request: Mapping[str, Any] | None = None,
        response: Any = None,
        generation_id: str | None = None,
        run_id: str | None = None,
    ) -> ReplayRecord:
        """Capture one live result produced by a ``LIVE`` replay fallback.

        This is deliberately separate from :meth:`record`: replay sessions do
        not record their ordinary activity. The appended record is marked
        consumed in this session, so a repeated key falls back live again rather
        than replaying the result it just captured.
        """

        if not self.is_replaying or self.fallback is not ReplayFallback.LIVE:
            raise RuntimeError("fallback capture requires replay mode with LIVE fallback")
        if kind not in (BoundaryKind.TOOL, BoundaryKind.MODEL):
            raise ValueError("fallback capture is supported only for tool and model boundaries")
        record = ReplayRecord(
            kind=kind,
            sequence=len(self.records),
            key=key,
            request=self.redactor.redact_value(dict(request or {})),
            response=self.redactor.redact_value(response),
            generation_id=generation_id,
            run_id=run_id,
        )
        self.records.append(record)
        cursor = f"{kind.value}:{key}"
        matches = sum(item.kind is kind and item.key == key for item in self.records)
        self._cursor[cursor] = matches
        return record

    def bind_redactor(self, redactor: SecretRedactor) -> None:
        """Adopt ``redactor`` and re-scrub everything already held.

        Called when a session is attached to a harness: an externally supplied
        recording must not carry secrets past the harness redaction boundary.
        """

        self.redactor = redactor
        self.metadata = redactor.redact_value(dict(self.metadata))
        self.records = [
            replace(
                record,
                request=redactor.redact_value(dict(record.request)),
                response=redactor.redact_value(record.response),
            )
            for record in self.records
        ]

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
        """Return the next recorded interaction for ``kind`` and ``key``.

        Consumption is per key: records of one boundary key replay in recording
        order, and out-of-order consumption across different keys never skips a
        record.

        Raises:
            ReplayMismatch: every record for this boundary is exhausted.
        """

        cursor = f"{kind.value}:{key}"
        matches = [record for record in self.records if record.kind is kind and record.key == key]
        consumed = self._cursor.get(cursor, 0)
        if consumed >= len(matches):
            reason = "exhausted" if matches else "missing"
            raise ReplayMismatch(
                f"no recorded {kind.value} interaction matches this operation"
                if not matches
                else f"recorded {kind.value} interactions for this key are exhausted",
                kind=kind.value,
                key=key,
                reason=reason,
                recorded=len([record for record in self.records if record.kind is kind]),
                consumed=consumed,
            )
        self._cursor[cursor] = consumed + 1
        return matches[consumed]

    def has(self, kind: BoundaryKind, *, key: str) -> bool:
        """Whether any record exists for this boundary, ignoring the cursor."""

        return any(record.kind is kind and record.key == key for record in self.records)

    def has_remaining(self, kind: BoundaryKind, *, key: str) -> bool:
        """Whether an unconsumed record exists for this boundary.

        Cursor-aware: a record already replayed for this key is exhausted and
        will not answer again.
        """

        return self.peek(kind, key=key) is not None

    def peek(self, kind: BoundaryKind, *, key: str) -> ReplayRecord | None:
        """The next unconsumed record for this boundary, without consuming it."""

        cursor = f"{kind.value}:{key}"
        matches = [record for record in self.records if record.kind is kind and record.key == key]
        consumed = self._cursor.get(cursor, 0)
        return matches[consumed] if consumed < len(matches) else None

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records:
            counts[record.kind.value] = counts.get(record.kind.value, 0) + 1
        return dict(sorted(counts.items()))

    # --------------------------------------------------------------- execution

    # ------------------------------------------------------------------ storage

    def to_dict(self) -> dict[str, Any]:
        """The recording as a self-describing document.

        Export is a boundary: secrets learned after a record was written are
        scrubbed here too. The document declares its own serialization format
        version — never the package version.
        """

        return {
            "format_version": REPLAY_FORMAT_VERSION,
            "mode": self.mode.value,
            "fallback": self.fallback.value,
            "metadata": self.redactor.redact_value(dict(self.metadata)),
            "records": [self.redactor.redact_value(record.to_dict()) for record in self.records],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], **overrides: Any) -> ReplaySession:
        """Rebuild a session from its serialized form.

        Dispatches explicitly on the declared format version. A pre-versioning
        0.8.1-era recording (which declared none) migrates and preserves every
        boundary kind and key, redaction status, and tool/model result semantics
        it recorded.

        Raises:
            FormatError: ``future_version``, ``malformed_version``,
                ``unmigratable``, or ``corrupted`` — a payload that does not
                match its declared shape is rejected, never reinterpreted.
        """

        if not isinstance(payload, Mapping):
            raise FormatError(
                "replay recording is corrupted: not a document",
                format="replay",
                reason="corrupted",
            )
        document = migrate_payload(
            payload,
            format_name="replay",
            supported=REPLAY_FORMAT_VERSION,
            migrations=_REPLAY_MIGRATIONS,
        )

        def corrupt(detail: str) -> FormatError:
            return FormatError(
                f"replay recording is corrupted: {detail}",
                format="replay",
                reason="corrupted",
            )

        # Every recording writer emits all four fields; a document missing one
        # is truncated, and a truncated recording must never degrade into an
        # empty live session that executes real boundaries.
        for required in ("mode", "fallback", "metadata", "records"):
            if required not in document:
                raise corrupt(f"missing {required!r}")

        try:
            mode = ReplayMode(document["mode"])
            fallback = ReplayFallback(document["fallback"])
        except (TypeError, ValueError) as error:
            raise corrupt(f"unknown mode or fallback: {error}") from error

        metadata = document["metadata"]
        raw_records = document["records"]
        if not isinstance(metadata, Mapping):
            raise corrupt("'metadata' is not a mapping")
        if not isinstance(raw_records, list):
            raise corrupt("'records' is not a list")

        arguments: dict[str, Any] = {
            "mode": mode,
            "fallback": fallback,
            "metadata": dict(metadata),
        }
        arguments.update(overrides)
        session = cls(**arguments)
        session.records = [_read_record(entry, index) for index, entry in enumerate(raw_records)]
        return session

    def save(self, path: str | Path) -> Path:
        """Write the recording as JSON."""

        target = Path(path)
        target.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path, **overrides: Any) -> ReplaySession:
        """Read a recording written by :meth:`save`."""

        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise FormatError(
                f"replay recording is corrupted: cannot read {Path(path).name}",
                format="replay",
                reason="corrupted",
            ) from error
        return cls.from_dict(payload, **overrides)


def _read_record(entry: Any, index: int) -> ReplayRecord:
    """One record with shape validation, rejecting rather than guessing."""

    if not isinstance(entry, Mapping):
        raise FormatError(
            f"replay recording is corrupted: record {index} is not a document",
            format="replay",
            reason="corrupted",
        )
    try:
        return ReplayRecord.from_dict(dict(entry))
    except (KeyError, TypeError, ValueError) as error:
        raise FormatError(
            f"replay recording is corrupted: record {index} is malformed ({error})",
            format="replay",
            reason="corrupted",
        ) from error


def _migrate_replay_v0(document: dict[str, Any]) -> dict[str, Any]:
    """Migrate a pre-versioning (0.8.1-era) recording to format 1.

    The 0.8.1 record shape is format 1's shape: migration preserves every
    boundary kind and key, request/response semantics, redaction status, and
    attribution field exactly, and stamps the format version.
    """

    return dict(document)


_REPLAY_MIGRATIONS = {0: _migrate_replay_v0}
