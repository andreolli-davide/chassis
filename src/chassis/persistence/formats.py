"""Explicit versioning for persisted and exchanged Chassis documents.

Every Chassis document that crosses a process, test-run, deployment, or
package-version boundary declares its own integer **format version** — never the
package version, because a serialization format has an independent
compatibility lifecycle. Readers dispatch explicitly:

* a known older version is migrated by a named migration step;
* the current version is read directly;
* anything else — a future version, a malformed version value, a corrupted
  payload, or a payload with no migration path — raises
  :class:`~chassis.core.errors.FormatError` with a machine-readable reason.

A payload that declared no format version is the pre-versioning 0.8.1-era shape
(version 0) and migrates through the same dispatcher. See
``docs/compatibility.md`` for the support horizon per format family.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from chassis.core.errors import FormatError

__all__ = [
    "DIAGNOSTICS_FORMAT_VERSION",
    "PLAN_FORMAT_VERSION",
    "REPLAY_FORMAT_VERSION",
    "SNAPSHOT_FORMAT_VERSION",
    "declared_format_version",
    "migrate_payload",
]

#: Runtime snapshot records (``RuntimeSnapshot``).
SNAPSHOT_FORMAT_VERSION = 1

#: Replay recordings (``ReplaySession``).
REPLAY_FORMAT_VERSION = 1

#: Reconciliation and diagnostics export documents.
DIAGNOSTICS_FORMAT_VERSION = 1

#: Planning export documents (``PlanResult``).
PLAN_FORMAT_VERSION = 1

#: A payload that declares no version: the pre-versioning (0.8.1-era) shape.
LEGACY_FORMAT_VERSION = 0

Migration = Callable[[dict[str, Any]], dict[str, Any]]


def _malformed(format_name: str, found: Any, supported: int) -> FormatError:
    return FormatError(
        f"{format_name} document declares a malformed format version",
        format=format_name,
        reason="malformed_version",
        found=found,
        supported=supported,
    )


def declared_format_version(
    payload: Mapping[str, Any],
    *,
    format_name: str,
    supported: int,
) -> int:
    """The format version a document declares, validated.

    A payload without ``format_version`` is the legacy pre-versioning shape and
    reports 0.

    Raises:
        FormatError: ``malformed_version`` for a non-integer or negative value,
            ``future_version`` for a version newer than this release supports.
    """

    if "format_version" not in payload:
        return LEGACY_FORMAT_VERSION
    found = payload["format_version"]
    if isinstance(found, bool) or not isinstance(found, int):
        raise _malformed(format_name, found, supported)
    if found < 0:
        raise _malformed(format_name, found, supported)
    if found > supported:
        raise FormatError(
            f"{format_name} document declares format version {found}, "
            f"newer than this release supports ({supported})",
            format=format_name,
            reason="future_version",
            found=found,
            supported=supported,
        )
    return found


def migrate_payload(
    payload: Mapping[str, Any],
    *,
    format_name: str,
    supported: int,
    migrations: Mapping[int, Migration],
) -> dict[str, Any]:
    """Bring a document to the supported format version through named steps.

    ``migrations[n]`` migrates a version-``n`` document to version ``n + 1``.
    A missing step is an explicit refusal, not a guess: the document is rejected
    as ``unmigratable``.
    """

    document = dict(payload)
    version = declared_format_version(document, format_name=format_name, supported=supported)
    while version < supported:
        step = migrations.get(version)
        if step is None:
            raise FormatError(
                f"{format_name} document at format version {version} has no migration "
                f"path to version {supported}",
                format=format_name,
                reason="unmigratable",
                found=version,
                supported=supported,
            )
        document = step(document)
        version += 1
    document["format_version"] = supported
    return document
