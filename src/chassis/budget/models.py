"""Budget dimensions, limits, usage, and *how* each dimension is enforced.

There are two kinds of budget dimension, and 0.2 makes the difference explicit
rather than leaving it to documentation:

* **enforced** -- Chassis owns the boundary where the dimension is consumed. Wall
  clock, tool calls, and child runs are charged (and refused) at the tool executor
  and the nested-run boundary. A configured limit is a guarantee.
* **accounted** -- the work happens *outside* any Chassis boundary: model calls,
  token usage, and cost are produced inside a graph. Chassis cannot observe them on
  its own, so a configured limit is *intent*: it is only charged when the code that
  owns the call reports it, canonically through
  :meth:`~chassis.budget.governor.BudgetGovernor.record`.

Every dimension is optional; ``None`` means unlimited. ``BudgetDimension.enforcement``
answers "who enforces this?" in code, and diagnostics carry the same fact, so a
token or cost limit can never be mistaken for a guarantee Chassis does not make.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from chassis.core.errors import ConfigurationError

__all__ = [
    "BudgetDimension",
    "BudgetEnforcement",
    "BudgetLimit",
    "BudgetLimits",
    "BudgetUsage",
]


class BudgetEnforcement(StrEnum):
    """Who is responsible for keeping a dimension within its limit."""

    #: Chassis observes the consumption itself and refuses at its own boundary.
    ENFORCED = "enforced"
    #: An integration must report consumption; the limit is intent until it does.
    ACCOUNTED = "accounted"


_ENFORCEMENT: dict[str, BudgetEnforcement] = {
    "wall_clock_seconds": BudgetEnforcement.ENFORCED,
    "tool_calls": BudgetEnforcement.ENFORCED,
    "child_runs": BudgetEnforcement.ENFORCED,
    "model_calls": BudgetEnforcement.ACCOUNTED,
    "tokens": BudgetEnforcement.ACCOUNTED,
    "estimated_cost": BudgetEnforcement.ACCOUNTED,
}


class BudgetDimension(StrEnum):
    """A measurable dimension of run consumption."""

    WALL_CLOCK_SECONDS = "wall_clock_seconds"
    MODEL_CALLS = "model_calls"
    TOOL_CALLS = "tool_calls"
    TOKENS = "tokens"
    ESTIMATED_COST = "estimated_cost"
    CHILD_RUNS = "child_runs"

    @property
    def enforcement(self) -> BudgetEnforcement:
        """Whether Chassis enforces this dimension or an integration accounts for it."""

        return _ENFORCEMENT[self.value]

    @property
    def is_enforced(self) -> bool:
        """Whether a configured limit on this dimension is a hard guarantee."""

        return self.enforcement is BudgetEnforcement.ENFORCED


@dataclass(frozen=True, slots=True)
class BudgetLimit:
    """One dimension's configured limit and its enforcement mode."""

    dimension: BudgetDimension
    limit: float | None
    enforcement: BudgetEnforcement

    @property
    def is_configured(self) -> bool:
        return self.limit is not None

    @property
    def is_enforced(self) -> bool:
        """A configured limit Chassis refuses at its own boundary."""

        return self.limit is not None and self.enforcement is BudgetEnforcement.ENFORCED

    @property
    def requires_accounting(self) -> bool:
        """A configured limit that only holds if an integration reports usage."""

        return self.limit is not None and self.enforcement is BudgetEnforcement.ACCOUNTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension.value,
            "limit": self.limit,
            "enforcement": self.enforcement.value,
        }


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Per-run limits. ``None`` disables a dimension.

    Enforced at a harness boundary and therefore a guarantee:
    ``wall_clock_seconds`` and ``tool_calls`` at tool execution, ``child_runs`` when
    a nested agent run starts. Accounted only, because the harness does not mediate
    the calls that would consume them, and therefore intent:
    ``model_calls``, ``tokens``, ``estimated_cost`` -- see
    :meth:`BudgetDimension.enforcement` and
    :meth:`~chassis.budget.governor.BudgetGovernor.record`.
    """

    wall_clock_seconds: float | None = None
    model_calls: int | None = None
    tool_calls: int | None = None
    tokens: int | None = None
    estimated_cost: float | None = None
    child_runs: int | None = None

    def __post_init__(self) -> None:
        """Reject invalid limits at construction.

        Limits must be non-negative and finite; count dimensions must be
        integers — fractions are rejected rather than truncated silently.
        """

        for dimension in BudgetDimension:
            limit = self.limit_for(dimension)
            if limit is None:
                continue
            if dimension in (
                BudgetDimension.MODEL_CALLS,
                BudgetDimension.TOOL_CALLS,
                BudgetDimension.TOKENS,
                BudgetDimension.CHILD_RUNS,
            ):
                if isinstance(limit, bool) or not isinstance(limit, int):
                    raise ConfigurationError(
                        "count budget limits must be integers",
                        dimension=dimension.value,
                        limit=limit,
                    )
            elif isinstance(limit, bool) or not isinstance(limit, (int, float)):
                raise ConfigurationError(
                    "budget limits must be numbers",
                    dimension=dimension.value,
                    limit=limit,
                )
            if limit < 0 or (isinstance(limit, float) and limit != limit) or limit == float("inf"):
                raise ConfigurationError(
                    "budget limits must be non-negative and finite",
                    dimension=dimension.value,
                    limit=limit,
                )

    def limit_for(self, dimension: BudgetDimension) -> float | None:
        match dimension:
            case BudgetDimension.WALL_CLOCK_SECONDS:
                return self.wall_clock_seconds
            case BudgetDimension.MODEL_CALLS:
                return self.model_calls
            case BudgetDimension.TOOL_CALLS:
                return self.tool_calls
            case BudgetDimension.TOKENS:
                return self.tokens
            case BudgetDimension.ESTIMATED_COST:
                return self.estimated_cost
            case BudgetDimension.CHILD_RUNS:
                return self.child_runs

    def spec_for(self, dimension: BudgetDimension) -> BudgetLimit:
        """The limit and enforcement mode of one dimension."""

        return BudgetLimit(
            dimension=dimension,
            limit=self.limit_for(dimension),
            enforcement=dimension.enforcement,
        )

    @property
    def is_unlimited(self) -> bool:
        return all(self.limit_for(dimension) is None for dimension in BudgetDimension)

    def enforced_dimensions(self) -> tuple[BudgetDimension, ...]:
        """Configured dimensions Chassis refuses at its own boundary."""

        return tuple(
            dimension for dimension in BudgetDimension if self.spec_for(dimension).is_enforced
        )

    def accounted_dimensions(self) -> tuple[BudgetDimension, ...]:
        """Configured dimensions that need an integration to report usage."""

        return tuple(
            dimension
            for dimension in BudgetDimension
            if self.spec_for(dimension).requires_accounting
        )

    @property
    def requires_accounting(self) -> bool:
        """Whether any configured limit only holds if an integration reports usage."""

        return bool(self.accounted_dimensions())

    def describe(self) -> dict[str, dict[str, Any]]:
        """Every dimension's limit and enforcement mode, configured or not."""

        return {
            dimension.value: self.spec_for(dimension).to_dict() for dimension in BudgetDimension
        }

    def to_dict(self) -> dict[str, float | int | None]:
        return {dimension.value: self.limit_for(dimension) for dimension in BudgetDimension}


@dataclass(slots=True)
class BudgetUsage:
    """Consumption recorded so far for one run."""

    model_calls: int = 0
    tool_calls: int = 0
    tokens: int = 0
    estimated_cost: float = 0.0
    child_runs: int = 0
    started_at: float | None = None
    extra: dict[str, float] = field(default_factory=dict)

    def consumed(self, dimension: BudgetDimension, *, now: float) -> float:
        match dimension:
            case BudgetDimension.WALL_CLOCK_SECONDS:
                return 0.0 if self.started_at is None else max(0.0, now - self.started_at)
            case BudgetDimension.MODEL_CALLS:
                return float(self.model_calls)
            case BudgetDimension.TOOL_CALLS:
                return float(self.tool_calls)
            case BudgetDimension.TOKENS:
                return float(self.tokens)
            case BudgetDimension.ESTIMATED_COST:
                return self.estimated_cost
            case BudgetDimension.CHILD_RUNS:
                return float(self.child_runs)

    def add(self, dimension: BudgetDimension, amount: float) -> None:
        match dimension:
            case BudgetDimension.MODEL_CALLS:
                self.model_calls += int(amount)
            case BudgetDimension.TOOL_CALLS:
                self.tool_calls += int(amount)
            case BudgetDimension.TOKENS:
                self.tokens += int(amount)
            case BudgetDimension.ESTIMATED_COST:
                self.estimated_cost += float(amount)
            case BudgetDimension.CHILD_RUNS:
                self.child_runs += int(amount)
            case BudgetDimension.WALL_CLOCK_SECONDS:
                # Elapsed time is observed, never accumulated.
                return

    def to_dict(self, *, now: float | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "tokens": self.tokens,
            "estimated_cost": self.estimated_cost,
            "child_runs": self.child_runs,
        }
        if now is not None:
            payload["wall_clock_seconds"] = self.consumed(
                BudgetDimension.WALL_CLOCK_SECONDS, now=now
            )
        return payload
