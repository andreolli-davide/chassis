from __future__ import annotations

import pytest

from chassis.budget import BudgetDimension, BudgetGovernor, BudgetLimits
from chassis.core.errors import BudgetExceeded


class FakeClock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_unlimited_governor_never_blocks() -> None:
    governor = BudgetGovernor()

    assert governor.limits.is_unlimited
    for _ in range(100):
        governor.consume(BudgetDimension.TOOL_CALLS)
    assert governor.consumed(BudgetDimension.TOOL_CALLS) == 100
    assert governor.remaining(BudgetDimension.TOOL_CALLS) is None
    assert governor.deadline() is None


def test_limit_is_enforced_on_the_boundary() -> None:
    governor = BudgetGovernor(BudgetLimits(tool_calls=2))

    governor.consume(BudgetDimension.TOOL_CALLS)
    governor.consume(BudgetDimension.TOOL_CALLS)

    with pytest.raises(BudgetExceeded) as excinfo:
        governor.consume(BudgetDimension.TOOL_CALLS)

    assert excinfo.value.context == {
        "dimension": "tool_calls",
        "limit": 2,
        "used": 2.0,
        "requested": 1.0,
    }
    assert governor.consumed(BudgetDimension.TOOL_CALLS) == 2


def test_check_does_not_consume() -> None:
    governor = BudgetGovernor(BudgetLimits(tokens=10))

    governor.check(BudgetDimension.TOKENS, amount=10)

    assert governor.consumed(BudgetDimension.TOKENS) == 0
    assert governor.remaining(BudgetDimension.TOKENS) == 10


def test_multiple_dimensions_are_independent() -> None:
    governor = BudgetGovernor(BudgetLimits(model_calls=1, tokens=100))

    governor.consume(BudgetDimension.TOKENS, amount=100)

    assert governor.remaining(BudgetDimension.TOKENS) == 0
    assert governor.remaining(BudgetDimension.MODEL_CALLS) == 1


def test_wall_clock_is_observed_not_accumulated() -> None:
    clock = FakeClock()
    governor = BudgetGovernor(BudgetLimits(wall_clock_seconds=5.0), clock=clock)

    clock.advance(3.0)
    assert governor.consumed(BudgetDimension.WALL_CLOCK_SECONDS) == 3.0
    assert governor.remaining(BudgetDimension.WALL_CLOCK_SECONDS) == 2.0
    assert governor.remaining_seconds(BudgetDimension.WALL_CLOCK_SECONDS) == 2.0
    governor.check(BudgetDimension.WALL_CLOCK_SECONDS)

    clock.advance(3.0)
    with pytest.raises(BudgetExceeded):
        governor.check(BudgetDimension.WALL_CLOCK_SECONDS)


def test_deadline_is_absolute() -> None:
    clock = FakeClock()
    governor = BudgetGovernor(BudgetLimits(wall_clock_seconds=5.0), clock=clock)

    assert governor.deadline() == 1005.0


def test_child_budget_consumption_counts_against_the_parent() -> None:
    parent = BudgetGovernor(BudgetLimits(tool_calls=3))

    first = parent.child(BudgetLimits(tool_calls=2))
    second = parent.child(BudgetLimits(tool_calls=2))

    first.consume(BudgetDimension.TOOL_CALLS, amount=2)
    assert parent.consumed(BudgetDimension.TOOL_CALLS) == 2
    assert first.remaining(BudgetDimension.TOOL_CALLS) == 0

    # The second child has its own allowance but only one shared unit is left.
    assert second.remaining(BudgetDimension.TOOL_CALLS) == 1
    second.consume(BudgetDimension.TOOL_CALLS)
    with pytest.raises(BudgetExceeded):
        second.consume(BudgetDimension.TOOL_CALLS)

    assert parent.consumed(BudgetDimension.TOOL_CALLS) == 3


def test_child_creation_consumes_a_child_run() -> None:
    parent = BudgetGovernor(BudgetLimits(child_runs=1))

    parent.child()

    assert parent.consumed(BudgetDimension.CHILD_RUNS) == 1
    with pytest.raises(BudgetExceeded):
        parent.child()


def test_grandchild_consumption_propagates_to_the_root() -> None:
    root = BudgetGovernor(BudgetLimits(tokens=100))
    child = root.child()
    grandchild = child.child()

    grandchild.consume(BudgetDimension.TOKENS, amount=60)

    assert root.consumed(BudgetDimension.TOKENS) == 60
    assert child.consumed(BudgetDimension.TOKENS) == 60
    assert grandchild.remaining(BudgetDimension.TOKENS) == 40

    with pytest.raises(BudgetExceeded):
        grandchild.consume(BudgetDimension.TOKENS, amount=41)


def test_cost_and_token_dimensions_accumulate() -> None:
    governor = BudgetGovernor(BudgetLimits(estimated_cost=0.05, tokens=1000))

    governor.consume(BudgetDimension.ESTIMATED_COST, amount=0.03)
    governor.consume(BudgetDimension.TOKENS, amount=400)

    assert governor.remaining(BudgetDimension.ESTIMATED_COST) == pytest.approx(0.02)
    assert governor.remaining(BudgetDimension.TOKENS) == 600


def test_diagnostics_report_limits_usage_and_remaining() -> None:
    governor = BudgetGovernor(BudgetLimits(tool_calls=5))
    governor.consume(BudgetDimension.TOOL_CALLS, amount=2)

    payload = governor.to_dict()

    assert payload["limits"]["tool_calls"] == 5
    assert payload["usage"]["tool_calls"] == 2
    assert payload["remaining"]["tool_calls"] == 3
    assert payload["remaining"]["tokens"] is None


def test_limits_serialization_is_complete() -> None:
    payload = BudgetLimits(tool_calls=1).to_dict()

    assert set(payload) == {dimension.value for dimension in BudgetDimension}


# --------------------------------------------------------------------------
# Construction-time validation (R018): reject invalid limits and amounts.
# --------------------------------------------------------------------------


def test_limits_reject_negative_and_non_finite_values() -> None:
    from chassis.core.errors import ConfigurationError

    for bad in (-1, float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ConfigurationError):
            BudgetLimits(wall_clock_seconds=bad)
        with pytest.raises(ConfigurationError):
            BudgetLimits(estimated_cost=bad)

    # Zero is a valid boundary.
    assert BudgetLimits(wall_clock_seconds=0.0, tool_calls=0).tool_calls == 0


def test_count_limits_require_integers() -> None:
    from chassis.core.errors import ConfigurationError

    for dimension in ("model_calls", "tool_calls", "tokens", "child_runs"):
        with pytest.raises(ConfigurationError):
            BudgetLimits(**{dimension: 1.5})  # type: ignore[arg-type]
        with pytest.raises(ConfigurationError):
            BudgetLimits(**{dimension: -2})
        assert BudgetLimits(**{dimension: 0}) is not None


def test_consumption_amounts_are_validated() -> None:
    from chassis.core.errors import ConfigurationError

    governor = BudgetGovernor(BudgetLimits(tool_calls=3, tokens=10, estimated_cost=1.0))

    governor.consume(BudgetDimension.TOOL_CALLS, amount=0)  # zero is a valid boundary
    for bad in (-1, float("inf"), float("nan")):
        with pytest.raises(ConfigurationError):
            governor.consume(BudgetDimension.TOOL_CALLS, amount=bad)
        with pytest.raises(ConfigurationError):
            governor.consume(BudgetDimension.ESTIMATED_COST, amount=bad)

    # Count dimensions never truncate fractions silently.
    with pytest.raises(ConfigurationError):
        governor.consume(BudgetDimension.TOKENS, amount=1.5)
    with pytest.raises(ConfigurationError):
        governor.record(tokens=2.5)  # type: ignore[arg-type]
    governor.record(tokens=2)
    assert governor.consumed(BudgetDimension.TOKENS) == 2
