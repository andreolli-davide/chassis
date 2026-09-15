"""Runtime generations: the consistency boundary observed by agent runs.

A generation is an immutable view of *composition* -- which plugins exist and
which capabilities they provide -- published atomically. Existing runs keep the
generation they acquired, so a provider can be swapped without mutating the
environment underneath them (invariants I4, I5).

The composition fields are frozen. The only mutable state is the accounting
object: the generation's lifecycle state and its lease count. Those describe the
generation's lifetime, not the environment it describes, and only the generation
manager mutates them.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from chassis.capabilities.snapshot import CapabilitySnapshot
from chassis.plugins.lifecycle import PluginInstance

__all__ = ["GenerationAccounting", "GenerationState", "RuntimeGeneration"]


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
    """

    state: GenerationState = GenerationState.BUILDING
    leases: int = 0
    zero: asyncio.Event = field(default_factory=asyncio.Event)

    def acquire(self) -> None:
        self.leases += 1

    def release(self) -> bool:
        """Release one lease; returns whether the generation just became idle."""

        self.leases -= 1
        if self.leases <= 0:
            self.leases = 0
            self.zero.set()
            return True
        return False

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
    def instance_ids(self) -> tuple[str, ...]:
        return tuple(instance.instance_id for instance in self.instances)

    def has_instance(self, instance_id: str) -> bool:
        return any(instance.instance_id == instance_id for instance in self.instances)

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
            "created_at": self.created_at,
            "instances": [instance.instance_id for instance in self.instances],
            "plugins": self.plugin_versions(),
            "capabilities": self.snapshot.versions(),
            "metadata": dict(self.metadata),
        }
