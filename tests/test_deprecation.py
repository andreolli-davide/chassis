"""Deprecations carry the full contract and point at the caller.

The compatibility policy (`docs/compatibility.md`) promises that a deprecated
API is announced through `ChassisDeprecationWarning` naming the API, its
replacement, the deprecating version, and the earliest removal version — at the
caller's frame, so filters and logs name the code that must change.
"""

from __future__ import annotations

import warnings
from typing import Any

import pytest

from chassis.compat import ChassisDeprecationWarning, deprecated, warn_deprecated


def _contract(record: pytest.WarningsRecorder) -> ChassisDeprecationWarning:
    """The typed warning instance from a ``pytest.warns`` recording."""

    message = record[0].message
    assert isinstance(message, ChassisDeprecationWarning)
    return message


def test_the_warning_category_is_typed_and_carries_the_contract() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warn_deprecated(
            api="Harness.plan",
            since="0.9.0",
            remove_in="1.0.0",
            replacement="Harness.preview()",
        )

    assert len(caught) == 1
    warning = caught[0]
    assert issubclass(ChassisDeprecationWarning, DeprecationWarning)
    assert warning.category is ChassisDeprecationWarning
    raised = warning.message
    assert isinstance(raised, ChassisDeprecationWarning)
    assert raised.api == "Harness.plan"
    assert raised.since == "0.9.0"
    assert raised.remove_in == "1.0.0"
    assert raised.replacement == "Harness.preview()"


def test_the_warning_message_names_every_required_fact() -> None:
    with pytest.warns(ChassisDeprecationWarning) as record:
        warn_deprecated(
            api="chassis.Scope.enter_context",
            since="0.9.0",
            remove_in="1.0.0",
            replacement="Scope.enter_async_context()",
        )

    message = str(record[0].message)
    assert "chassis.Scope.enter_context" in message
    assert "0.9.0" in message
    assert "1.0.0" in message
    assert "Scope.enter_async_context()" in message


def test_a_deprecation_without_a_replacement_says_so() -> None:
    with pytest.warns(ChassisDeprecationWarning) as record:
        warn_deprecated(api="chassis.legacy", since="0.9.0", remove_in="1.0.0")

    message = str(record[0].message)
    assert "no replacement" in message
    assert _contract(record).replacement is None


def test_the_warning_points_at_the_callers_frame() -> None:
    @deprecated(since="0.9.0", remove_in="1.0.0", replacement="new_api()")
    def old_api() -> str:
        return "result"

    with pytest.warns(ChassisDeprecationWarning) as record:
        assert old_api() == "result"

    assert record[0].filename == __file__


def test_deprecated_functions_keep_their_behavior_and_signature() -> None:
    @deprecated(since="0.9.0", remove_in="1.0.0", name="documented.name")
    def add(a: int, b: int = 1) -> int:
        """Add two numbers."""

        return a + b

    with pytest.warns(ChassisDeprecationWarning) as record:
        assert add(2) == 3
        assert add(2, b=5) == 7

    assert add.__name__ == "add"
    assert add.__doc__ == "Add two numbers."
    assert str(record[0].message).startswith("documented.name is deprecated")


def test_deprecated_methods_warn_once_per_call() -> None:
    class Service:
        @deprecated(since="0.9.0", remove_in="1.0.0", replacement="Service.run()")
        def execute(self) -> str:
            return "done"

    service = Service()
    with pytest.warns(ChassisDeprecationWarning) as record:
        assert service.execute() == "done"
        assert service.execute() == "done"

    assert len(record) == 2
    assert "Service.run()" in str(record[0].message)


def test_a_deprecated_class_warns_at_construction() -> None:
    @deprecated(
        since="0.9.0", remove_in="1.0.0", replacement="NewClient()", name="chassis.OldClient"
    )
    class OldClient:
        def __init__(self, token: str) -> None:
            self.token = token

    with pytest.warns(ChassisDeprecationWarning) as record:
        client = OldClient("value")

    assert client.token == "value"
    contract = _contract(record)
    assert record[0].filename == __file__
    assert contract.api == "chassis.OldClient"
    assert contract.since == "0.9.0"
    assert contract.remove_in == "1.0.0"
    assert contract.replacement == "NewClient()"


def test_without_the_decorator_no_warning_is_ever_emitted() -> None:
    def quiet(value: Any) -> Any:
        return value

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        quiet(1)

    assert caught == []
