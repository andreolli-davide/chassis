"""Runtime generations: the consistency boundary observed by agent runs.

A generation is an immutable view of *composition* -- which plugins exist and
which capabilities they provide -- published atomically. Existing runs keep the
generation they acquired, so a provider can be swapped without mutating the
environment underneath them (invariants I4, I5).

The composition fields are frozen. The only mutable state is the accounting
object: the generation's lifecycle state and its outstanding leases. Those describe
the generation's lifetime, not the environment it describes, and only the
generation manager mutates them.

Leases carry identity and a start time. That is what makes generation pressure
observable -- how old the oldest outstanding lease is, and which generation is
retaining resources for how long -- without the data path taking any lock:
acquiring and releasing a lease are synchronous operations with no ``await``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from chassis.capabilities.snapshot import CapabilitySnapshot
from chassis.composition import ScopeTree
from chassis.core.errors import UnknownLeaseError
from chassis.core.identity import SemanticIdentity
from chassis.plugins.lifecycle import PluginInstance

__all__ = [
    "GenerationAccounting",
    "GenerationLease",
    "GenerationState",
    "RuntimeGeneration",
]


class GenerationState(StrEnum):
    """Lifecycle of a runtime generation."""

    BUILDING = "building"
    ACTIVE = "active"
    DRAINING = "draining"
    RETIRED = "retired"


@dataclass(slots=True)
class GenerationAccounting:
    """Mutable lifetime accounting for one generation.

    Acquire and release never await, which lets the data plane take a lease
    without touching the control-plane lock and without a cancellation hazard.

    Every lease records when it started, so an operator can tell how long a
    draining generation has been kept alive and by what. The start times are keyed
    by a lease id rather than by acquisition order, so a lease released out of
    order still leaves the *actual* oldest outstanding lease visible.
    """

    state: GenerationState = GenerationState.BUILDING
    leases: int = 0
    zero: asyncio.Event = field(default_factory=asyncio.Event)
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    _starts: dict[int, float] = field(default_factory=dict, repr=False)
    _next_lease: int = field(default=0, repr=False)

    def acquire(self) -> int:
        """Record one lease and return its id.

        The idle event is cleared on the ``0 -> 1`` transition, so a waiter that
        joins after a reacquisition waits for the *current* cycle to go idle
        instead of observing the stale signal from the previous one.
        """

        if self.leases == 0:
            self.zero.clear()
        lease_id = self._next_lease
        self._next_lease += 1
        self._starts[lease_id] = self.clock()
        self.leases += 1
        return lease_id

    def release(self, lease_id: int) -> bool:
        """Release one lease; returns whether the generation just became idle.

        The lease id must be outstanding. An unknown id and a duplicate release
        are both rejected with :class:`UnknownLeaseError`, and accounting is never
        altered by such a release.
        """

        if lease_id not in self._starts:
            raise UnknownLeaseError(
                "lease is not outstanding",
                lease_id=lease_id,
            )
        del self._starts[lease_id]
        self.leases -= 1
        if self.leases == 0:
            self.zero.set()
            return True
        return False

    @property
    def oldest_lease_started_at(self) -> float | None:
        """Monotonic start time of the oldest outstanding lease, if any."""

        return min(self._starts.values()) if self._starts else None

    def oldest_lease_age_seconds(self) -> float | None:
        """Age of the oldest outstanding lease, or ``None`` when unleased."""

        started = self.oldest_lease_started_at
        return None if started is None else max(0.0, self.clock() - started)

    def lease_ages_seconds(self) -> tuple[float, ...]:
        """Age of every outstanding lease, oldest first."""

        now = self.clock()
        return tuple(sorted(max(0.0, now - started) for started in self._starts.values()))

    async def wait_idle(self) -> None:
        """Wait until no lease remains."""

        if self.leases == 0:
            return
        await self.zero.wait()


@dataclass(frozen=True, slots=True, eq=False)
class RuntimeGeneration:
    """An immutable, published view of runtime composition."""

    generation_id: str
    sequence: int
    snapshot: CapabilitySnapshot
    instances: tuple[PluginInstance, ...]
    accounting: GenerationAccounting = field(compare=False, repr=False)
    created_at: float = field(default_factory=time.time)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    scopes: ScopeTree = field(default_factory=ScopeTree.root_only)

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def state(self) -> GenerationState:
        return self.accounting.state

    @property
    def lease_count(self) -> int:
        return self.accounting.leases

    @property
    def oldest_lease_age_seconds(self) -> float | None:
        """Age of the oldest outstanding lease, or ``None`` when unleased."""

        return self.accounting.oldest_lease_age_seconds()

    @property
    def instance_ids(self) -> tuple[str, ...]:
        return tuple(instance.instance_id for instance in self.instances)

    def has_instance(self, instance_id: str) -> bool:
        return any(instance.instance_id == instance_id for instance in self.instances)

    @property
    def identities(self) -> Mapping[str, SemanticIdentity]:
        """Semantic identity of every mounted node, keyed by entry id.

        Derived from the instances the generation was published with, so it is a
        view of the immutable composition rather than stored state. An instance
        that predates semantic identity contributes nothing.
        """

        return MappingProxyType(
            {
                instance.entry_id: instance.semantic_identity
                for instance in self.instances
                if instance.semantic_identity is not None
            }
        )

    def identity_for(self, entry_id: str) -> SemanticIdentity | None:
        """Semantic identity of one node, or ``None`` when it is not published."""

        return self.identities.get(entry_id)

    def plugin_versions(self) -> dict[str, str]:
        """Plugin name to version for the plugins in this generation."""

        return {instance.manifest.name: instance.manifest.version for instance in self.instances}

    def to_dict(self) -> dict[str, Any]:
        """Diagnostic view. Configuration values are never included."""

        return {
            "generation_id": self.generation_id,
            "sequence": self.sequence,
            "state": self.state.value,
            "leases": self.lease_count,
            "oldest_lease_age_seconds": self.oldest_lease_age_seconds,
            "created_at": self.created_at,
            "instances": [instance.instance_id for instance in self.instances],
            "plugins": self.plugin_versions(),
            "capabilities": self.snapshot.versions(),
            "metadata": dict(self.metadata),
            "scopes": self.scopes.to_dict(),
        }


@dataclass(frozen=True, slots=True, eq=False)
class GenerationLease:
    """One run's hold on one runtime generation.

    Returned by :meth:`~chassis.core.generations.GenerationManager.acquire_lease`
    and released by ``release_lease``. Carrying the lease explicitly is what makes
    lease age authoritative: the generation knows *which* hold was released, not
    merely how many remain.
    """

    generation: RuntimeGeneration
    lease_id: int
    started_at: float

    @property
    def generation_id(self) -> str:
        return self.generation.generation_id

    @property
    def age_seconds(self) -> float:
        """How long this lease has been held."""

        return max(0.0, self.generation.accounting.clock() - self.started_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "generation_id": self.generation.generation_id,
            "age_seconds": self.age_seconds,
        }
