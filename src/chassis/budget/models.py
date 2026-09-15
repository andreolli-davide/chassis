"""Budget dimensions, limits, and usage.

Budgets are enforced only at boundaries Chassis actually controls: a tool call it
mediates, a model call it invokes, a child run it starts. Nothing here promises to
stop work happening outside those boundaries.

The initial model covers the dimensions a harness can genuinely account for.
Every dimension is optional; ``None`` means unlimited.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = ["BudgetDimension", "BudgetLimits", "BudgetUsage"]


class BudgetDimension(StrEnum):
    """A measurable dimension of run consumption."""

    WALL_CLOCK_SECONDS = "wall_clock_seconds"
    MODEL_CALLS = "model_calls"
    TOOL_CALLS = "tool_calls"
    TOKENS = "tokens"
    ESTIMATED_COST = "estimated_cost"
    CHILD_RUNS = "child_runs"


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Per-run limits. ``None`` disables a dimension."""

    wall_clock_seconds: float | None = None
    model_calls: int | None = None
    tool_calls: int | None = None
    tokens: int | None = None
    estimated_cost: float | None = None
    child_runs: int | None = None

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

    @property
    def is_unlimited(self) -> bool:
        return all(self.limit_for(dimension) is None for dimension in BudgetDimension)

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
