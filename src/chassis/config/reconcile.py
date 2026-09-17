"""Desired-state reconciliation.

The reconciler compares desired configuration against installed entries and
derives operations. It does not mutate anything itself: the harness applies the
operations through the same ``install``/``uninstall``/``reconcile`` path as
programmatic use, so there is exactly one implementation of composition.

``RECONFIGURE`` is part of the vocabulary but is never emitted today. A
configuration change conservatively becomes ``REPLACE``, because in-place mutation
of a running plugin cannot be made safe while older generations may still depend
on the previous semantics.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from chassis.config.models import HarnessConfig

__all__ = [
    "DesiredStateAction",
    "DesiredStateChange",
    "InstalledEntry",
    "config_fingerprint",
    "diff_desired_state",
]


class DesiredStateAction(StrEnum):
    """Operation derived for one plugin entry."""

    ADD = "add"
    REMOVE = "remove"
    UNCHANGED = "unchanged"
    REPLACE = "replace"
    #: Reserved for a future in-place reconfigure protocol; never emitted today.
    RECONFIGURE = "reconfigure"


@dataclass(frozen=True, slots=True)
class DesiredStateChange:
    """One derived operation."""

    action: DesiredStateAction
    entry_id: str
    plugin: str | None = None
    reason: str = ""

    @property
    def is_mutation(self) -> bool:
        return self.action in (
            DesiredStateAction.ADD,
            DesiredStateAction.REMOVE,
            DesiredStateAction.REPLACE,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "entry_id": self.entry_id,
            "plugin": self.plugin,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class InstalledEntry:
    """What the harness currently has installed for one entry id."""

    entry_id: str
    plugin: str
    revision: int
    config_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "plugin": self.plugin,
            "revision": self.revision,
        }


def diff_desired_state(
    desired: HarnessConfig,
    installed: Mapping[str, InstalledEntry],
) -> tuple[DesiredStateChange, ...]:
    """Derive operations to move from ``installed`` to ``desired``.

    Results are ordered deterministically by entry id, so a reconciliation run is
    reproducible and its diagnostics are stable.
    """

    wanted = {entry.id: entry for entry in desired.enabled_entries}
    changes: list[DesiredStateChange] = []

    for entry_id in sorted(set(wanted) | set(installed)):
        entry = wanted.get(entry_id)
        current = installed.get(entry_id)
        if entry is not None and current is None:
            changes.append(
                DesiredStateChange(
                    action=DesiredStateAction.ADD,
                    entry_id=entry_id,
                    plugin=entry.plugin,
                    reason="entry is desired but not installed",
                )
            )
            continue
        if entry is None and current is not None:
            changes.append(
                DesiredStateChange(
                    action=DesiredStateAction.REMOVE,
                    entry_id=entry_id,
                    plugin=current.plugin,
                    reason="entry is installed but no longer desired",
                )
            )
            continue
        assert entry is not None and current is not None
        if entry.plugin != current.plugin:
            changes.append(
                DesiredStateChange(
                    action=DesiredStateAction.REPLACE,
                    entry_id=entry_id,
                    plugin=entry.plugin,
                    reason=f"implementation changed from {current.plugin!r}",
                )
            )
            continue
        if (
            config_fingerprint(plugin=entry.plugin, config=entry.config)
            != current.config_fingerprint
        ):
            changes.append(
                DesiredStateChange(
                    action=DesiredStateAction.REPLACE,
                    entry_id=entry_id,
                    plugin=entry.plugin,
                    reason="configuration changed; in-place reconfigure is not supported",
                )
            )
            continue
        changes.append(
            DesiredStateChange(
                action=DesiredStateAction.UNCHANGED,
                entry_id=entry_id,
                plugin=entry.plugin,
            )
        )

    return tuple(changes)


def config_fingerprint(
    *,
    plugin: str,
    config: Mapping[str, Any],
) -> str:
    """Stable identity of an entry's effective configuration.

    Shared by the reconciler's desired side and the harness's installed side so the
    two can never disagree about whether a configuration changed.

    Provider preferences are deliberately *not* part of this identity. They
    disambiguate resolution rather than configure the plugin, and 0.4 resolves
    dependencies on every reconciliation: a preference change is applied by
    :meth:`~chassis.harness.Harness.prefer_provider` and reported through the
    consumer's dependency bindings, never as a configuration change. Including it
    here made a re-applied declarative configuration look like it had changed.
    """

    from chassis.persistence.hashing import stable_hash

    return stable_hash(
        {
            "plugin": plugin,
            "config": dict(config),
        }
    )
