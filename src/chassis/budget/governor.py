"""Budget accounting and enforcement for one run.

A governor tracks consumption against limits and raises
:class:`~chassis.core.errors.BudgetExceeded` at a controlled boundary. Child runs
get a child governor; consumption propagates upward so a parent can never be
overdrawn by its children, and a child can never exceed what the parent has left.

Governors are not thread-safe. A run's governor is mutated from the task driving
that run, which is the only writer.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from chassis.budget.models import BudgetDimension, BudgetLimits, BudgetUsage
from chassis.core.errors import BudgetExceeded

__all__ = ["BudgetGovernor"]


class BudgetGovernor:
    """Tracks and enforces one allocation of a budget.

    Args:
        limits: Limits for this allocation. ``None`` means unlimited.
        parent: Enclosing allocation. Consumption counts against both.
        clock: Monotonic clock, injectable for deterministic tests.
    """

    def __init__(
        self,
        limits: BudgetLimits | None = None,
        *,
        parent: BudgetGovernor | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = limits or BudgetLimits()
        self._parent = parent
        self._clock = clock
        self._usage = BudgetUsage(started_at=clock())

    @property
    def limits(self) -> BudgetLimits:
        return self._limits

    @property
    def usage(self) -> BudgetUsage:
        return self._usage

    @property
    def parent(self) -> BudgetGovernor | None:
        return self._parent

    def now(self) -> float:
        return self._clock()

    def consumed(self, dimension: BudgetDimension) -> float:
        """Amount consumed by this allocation and its children."""

        return self._usage.consumed(dimension, now=self._clock())

    def remaining(self, dimension: BudgetDimension) -> float | None:
        """Remaining allowance, considering the parent chain. ``None`` if unlimited."""

        candidates: list[float] = []
        local_limit = self._limits.limit_for(dimension)
        if local_limit is not None:
            candidates.append(local_limit - self._usage.consumed(dimension, now=self._clock()))
        if self._parent is not None:
            parent_remaining = self._parent.remaining(dimension)
            if parent_remaining is not None:
                candidates.append(parent_remaining)
        if not candidates:
            return None
        return max(0.0, min(candidates))

    def deadline(self) -> float | None:
        """Absolute time at which the wall-clock budget expires, if limited."""

        limit = self._limits.wall_clock_seconds
        if limit is None:
            return None
        started = self._usage.started_at
        if started is None:  # pragma: no cover - started_at is always set
            return None
        return started + limit

    def remaining_seconds(self, dimension: BudgetDimension) -> float | None:
        """Remaining time before the wall-clock budget is exhausted."""

        if dimension is not BudgetDimension.WALL_CLOCK_SECONDS:
            raise ValueError("remaining_seconds only applies to the wall-clock dimension")
        deadline = self.deadline()
        if deadline is None:
            parent_remaining = (
                None if self._parent is None else self._parent.remaining_seconds(dimension)
            )
            return parent_remaining
        own = max(0.0, deadline - self._clock())
        if self._parent is None:
            return own
        parent_remaining = self._parent.remaining_seconds(dimension)
        return own if parent_remaining is None else min(own, parent_remaining)

    def check(self, dimension: BudgetDimension, *, amount: float = 1.0) -> None:
        """Raise :class:`BudgetExceeded` if ``amount`` would exceed the budget."""

        limit = self._limits.limit_for(dimension)
        if limit is not None:
            used = self._usage.consumed(dimension, now=self._clock())
            if used + amount > limit:
                raise BudgetExceeded(
                    f"budget exceeded for {dimension.value}",
                    dimension=dimension.value,
                    limit=limit,
                    used=used,
                    requested=amount,
                )
        if self._parent is not None:
            self._parent.check(dimension, amount=amount)

    def consume(self, dimension: BudgetDimension, *, amount: float = 1.0) -> None:
        """Check then record consumption. Raises if the budget is exhausted."""

        self.check(dimension, amount=amount)
        self._usage.add(dimension, amount)
        if self._parent is not None:
            self._parent._record(dimension, amount)

    def _record(self, dimension: BudgetDimension, amount: float) -> None:
        self._usage.add(dimension, amount)
        if self._parent is not None:
            self._parent._record(dimension, amount)

    def child(self, limits: BudgetLimits | None = None) -> BudgetGovernor:
        """Allocate a sub-budget for a child run."""

        self.consume(BudgetDimension.CHILD_RUNS)
        return BudgetGovernor(limits, parent=self, clock=self._clock)

    def to_dict(self) -> dict[str, Any]:
        return {
            "limits": self._limits.to_dict(),
            "usage": self._usage.to_dict(now=self._clock()),
            "remaining": {
                dimension.value: self.remaining(dimension) for dimension in BudgetDimension
            },
        }
