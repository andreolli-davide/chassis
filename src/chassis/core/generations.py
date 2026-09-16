"""Publication, acquisition, draining, and retirement of runtime generations.

Concurrency model
-----------------

Publication, acquisition, and release are each a *synchronous* sequence with no
``await`` between their read and their write. In an asyncio program that makes
them atomic with respect to every other task, which is why the data path takes no
lock at all: a run reads the current generation and takes its lease in the same
scheduling slice, so it can never observe a half-published generation nor lease a
generation that is about to be retired.

The control plane still serializes *composition* -- mounting, rollback, and
disposal -- through the harness lock. Nothing here serializes model or tool calls
(invariant I8).

Retirement is lease-driven: a generation that is no longer current becomes
DRAINING, and only once its last lease is released may it retire and let plugins
that no live generation can reach be disposed (invariant I6).

Liveness and diagnostics are tracked separately. A draining generation stays live
until it is retired, no matter how many newer generations are published, because
the bounded history buffer only ever evicts *retired* generations. Deriving
liveness from that buffer would let a long-running lease lose its generation and
have its resources disposed underneath it.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from chassis.capabilities.snapshot import CapabilitySnapshot
from chassis.core.errors import GenerationConflictError, HarnessStateError
from chassis.core.generation import (
    GenerationAccounting,
    GenerationState,
    RuntimeGeneration,
)
from chassis.plugins.lifecycle import PluginInstance

__all__ = ["GenerationManager"]

_HISTORY_LIMIT = 32


class GenerationManager:
    """Owns the sequence of runtime generations and their lease accounting.

    Args:
        history_limit: How many *retired* generations to retain for diagnostics.
            Live generations (current and draining) are never evicted by it.
    """

    def __init__(self, *, history_limit: int = _HISTORY_LIMIT) -> None:
        self._current: RuntimeGeneration | None = None
        self._draining: dict[str, RuntimeGeneration] = {}
        self._retired: list[RuntimeGeneration] = []
        self._history_limit = history_limit
        self._counter = itertools.count(1)

    # ------------------------------------------------------------------ reading

    @property
    def current(self) -> RuntimeGeneration | None:
        """The generation new runs acquire."""

        return self._current

    def live(self) -> tuple[RuntimeGeneration, ...]:
        """Generations still reachable by an active run: ACTIVE and DRAINING."""

        generations: list[RuntimeGeneration] = []
        if self._current is not None:
            generations.append(self._current)
        generations.extend(self._draining.values())
        return tuple(generations)

    def draining(self) -> tuple[RuntimeGeneration, ...]:
        """Generations waiting for their last run to finish."""

        return tuple(self._draining.values())

    @property
    def history(self) -> tuple[RuntimeGeneration, ...]:
        """Retired generations retained for diagnostics, newest first."""

        return tuple(reversed(self._retired))

    def all_generations(self) -> tuple[RuntimeGeneration, ...]:
        """Every generation still known, newest first.

        Live generations are always included; only retired diagnostics are bounded.
        """

        return tuple(
            sorted(
                self.live() + tuple(self._retired),
                key=lambda generation: generation.sequence,
                reverse=True,
            )
        )

    def reachable_instance_ids(self) -> frozenset[str]:
        """Instance ids reachable from a live generation.

        A plugin absent from the current generation but present in a draining one
        is still reachable, and therefore still alive.
        """

        reachable: set[str] = set()
        for generation in self.live():
            reachable.update(generation.instance_ids)
        return frozenset(reachable)

    # ------------------------------------------------------------------ control

    def build(
        self,
        *,
        snapshot_factory: Callable[[str], CapabilitySnapshot],
        instances: Iterable[PluginInstance],
        metadata: Mapping[str, Any] | None = None,
    ) -> RuntimeGeneration:
        """Create a candidate generation. It is invisible until published.

        ``snapshot_factory`` receives the new generation id so that the immutable
        capability snapshot can name its own generation.
        """

        sequence = next(self._counter)
        generation_id = f"gen_{sequence:04d}"
        return RuntimeGeneration(
            generation_id=generation_id,
            sequence=sequence,
            snapshot=snapshot_factory(generation_id),
            instances=tuple(instances),
            accounting=GenerationAccounting(),
            metadata=dict(metadata or {}),
        )

    def publish(self, generation: RuntimeGeneration) -> RuntimeGeneration | None:
        """Atomically make ``generation`` current and mark the old one DRAINING.

        Returns the previous generation, if any.

        The outgoing generation is marked DRAINING *before* the current pointer is
        swapped. An acquirer therefore either sees the old generation while it is
        still active (and takes its lease immediately) or sees the new one; it
        cannot take a lease on a generation that is already retiring.
        """

        if generation.state is not GenerationState.BUILDING:
            raise GenerationConflictError(
                "only a building generation can be published",
                generation_id=generation.generation_id,
                state=generation.state.value,
            )
        previous = self._current
        if previous is not None and previous is not generation:
            previous.accounting.state = GenerationState.DRAINING
            self._draining[previous.generation_id] = previous
        generation.accounting.state = GenerationState.ACTIVE
        self._current = generation
        return previous

    def retire(self, generation: RuntimeGeneration) -> bool:
        """Retire a draining generation. Returns whether the state changed."""

        state = generation.state
        if state is GenerationState.RETIRED:
            return False
        if state is GenerationState.ACTIVE:
            raise GenerationConflictError(
                "cannot retire the current generation while it is active",
                generation_id=generation.generation_id,
            )
        generation.accounting.state = GenerationState.RETIRED
        self._draining.pop(generation.generation_id, None)
        self._retired.append(generation)
        del self._retired[: max(0, len(self._retired) - self._history_limit)]
        if self._current is generation:  # pragma: no cover - guarded above
            self._current = None
        return True

    def discard(self, generation: RuntimeGeneration) -> bool:
        """Drop a candidate that will never be published, without recording it."""

        if generation.state is GenerationState.ACTIVE:
            raise GenerationConflictError(
                "cannot discard the active generation",
                generation_id=generation.generation_id,
            )
        generation.accounting.state = GenerationState.RETIRED
        return True

    def begin_shutdown(self) -> tuple[RuntimeGeneration, ...]:
        """Stop accepting new runs and mark the current generation draining.

        Returns the generations that must still be waited for.
        """

        current = self._current
        if current is not None:
            current.accounting.state = GenerationState.DRAINING
            self._draining[current.generation_id] = current
            self._current = None
        return self.draining()

    # ------------------------------------------------------------- acquisition

    def acquire(self) -> RuntimeGeneration:
        """Take a lease on the current generation.

        The read and the increment happen without an intervening ``await``, so the
        acquired generation is coherent and cannot be retired out from under the
        caller.
        """

        generation = self._current
        if generation is None or generation.state is not GenerationState.ACTIVE:
            raise HarnessStateError("no runtime generation is available")
        generation.accounting.acquire()
        return generation

    def release(self, generation: RuntimeGeneration) -> bool:
        """Release a lease.

        Returns whether this was the last lease of a draining generation, which is
        the signal that retirement and disposal may proceed.
        """

        became_idle = generation.accounting.release()
        return became_idle and generation.state is GenerationState.DRAINING

    async def drain(
        self,
        generations: Iterable[RuntimeGeneration],
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[tuple[RuntimeGeneration, ...], tuple[RuntimeGeneration, ...]]:
        """Wait for generations to become idle.

        Returns ``(idle, busy)``. Nothing is retired here, so the caller stays in
        control of when resources are reclaimed.
        """

        idle: list[RuntimeGeneration] = []
        busy: list[RuntimeGeneration] = []
        for generation in generations:
            if generation.lease_count == 0:
                idle.append(generation)
                continue
            try:
                if timeout_seconds is None:
                    await generation.accounting.wait_idle()
                else:
                    async with asyncio.timeout(timeout_seconds):
                        await generation.accounting.wait_idle()
            except TimeoutError:
                busy.append(generation)
            else:
                idle.append(generation)
        return tuple(idle), tuple(busy)

    # ------------------------------------------------------------- bookkeeping

    def refresh_references(self, instances: Iterable[PluginInstance]) -> None:
        """Recompute how many live generations reach each instance.

        Disposal is gated on this count, so it is recomputed from the authoritative
        generation set instead of being incremented and decremented in several
        places.
        """

        live = self.live()
        for instance in instances:
            instance.generation_refs = sum(
                1 for generation in live if generation.has_instance(instance.instance_id)
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": None if self._current is None else self._current.to_dict(),
            "draining": [generation.to_dict() for generation in self.draining()],
            "history": [generation.to_dict() for generation in self.history],
        }
