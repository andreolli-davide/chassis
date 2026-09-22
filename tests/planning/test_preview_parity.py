"""Preview decisions match the subsequent real apply.

The parity proof for the planning contract: every previewed action is checked
against what `apply_config` + `reconcile` actually mounted, reused, or disposed,
and against the generation-impact analysis the reconcile reports. What preview
claims is what apply does — including dependency cascades and no-op reconciles.
"""

from __future__ import annotations

from typing import Any

from tests.planning.support import cache_provider, db_provider, memory_consumer

from chassis import Harness


def catalogued() -> Harness:
    harness = Harness()
    harness.register_plugin_type("orders-db", db_provider)
    harness.register_plugin_type("memory", memory_consumer)
    return harness


def actions_by_entry(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["entry_id"]: item for item in payload["actions"] if item["entry_id"] is not None}


INITIAL = {
    "version": 1,
    "plugins": [
        {"id": "orders-db", "plugin": "orders-db"},
        {"id": "memory", "plugin": "memory"},
    ],
}

REPLACEMENT = {
    "version": 1,
    "plugins": [
        {"id": "orders-db", "plugin": "orders-db", "config": {"pool": 2}},
        {"id": "memory", "plugin": "memory"},
    ],
}


async def test_preview_of_a_configuration_matches_the_apply() -> None:
    harness = catalogued()

    preview = harness.preview(INITIAL)
    payload = preview.to_dict()
    planned = actions_by_entry(payload)

    assert planned["orders-db"]["action"] == "add"
    assert planned["memory"]["action"] == "add"
    assert payload["would_publish"] is True
    assert payload["actions"][-1]["action"] == "publish"

    harness.apply_config(INITIAL)
    result = await harness.reconcile()

    assert set(result.mounted) == {"orders-db", "memory"}
    assert result.disposed == ()
    assert preview.current_generation_id is None
    assert result.generation_id != preview.current_generation_id


async def test_preview_predicts_the_replacement_cascade() -> None:
    harness = catalogued()
    harness.apply_config(INITIAL)
    await harness.start()
    try:
        preview = harness.preview(REPLACEMENT)
        payload = preview.to_dict()
        planned = actions_by_entry(payload)

        assert planned["orders-db"]["action"] == "replace"
        assert planned["orders-db"]["reasons"] == ["configuration_changed"]
        cascade = planned["memory"]
        assert cascade["action"] == "rebuild"
        assert cascade["expected_reuse"] is False
        assert cascade["cause_capability"] == "database"
        assert cascade["cause_provider"] == "orders-db"
        assert "dependency_changed" in cascade["reasons"]
        assert payload["would_publish"] is True

        harness.apply_config(REPLACEMENT)
        result = await harness.reconcile()

        assert set(result.mounted) == {"orders-db", "memory"}
        assert result.reused == ()
        assert result.impact is not None
        assert result.impact.get("orders-db") is not None
        assert result.impact.get("orders-db").reused is False  # type: ignore[union-attr]
        assert result.impact.get("memory").reused is False  # type: ignore[union-attr]
    finally:
        await harness.stop()


async def test_preview_matches_a_no_op_reconcile() -> None:
    harness = catalogued()
    harness.apply_config(INITIAL)
    started = await harness.start()
    try:
        preview = harness.preview()
        payload = preview.to_dict()
        planned = actions_by_entry(payload)

        assert planned["orders-db"]["action"] == "reuse"
        assert planned["memory"]["action"] == "reuse"
        assert planned["memory"]["expected_reuse"] is True
        assert payload["would_publish"] is False
        assert payload["actions"][-1]["action"] == "no-op"

        result = await harness.reconcile()

        assert result.generation_id == started.generation_id
        assert result.generation_id == preview.current_generation_id
        assert result.mounted == ()
        assert set(result.reused) == {"orders-db", "memory"}
    finally:
        await harness.stop()


async def test_preview_matches_a_removal() -> None:
    harness = catalogued()
    harness.apply_config(INITIAL)
    await harness.start()
    try:
        remaining = {"version": 1, "plugins": [{"id": "orders-db", "plugin": "orders-db"}]}
        preview = harness.preview(remaining)
        payload = preview.to_dict()
        planned = actions_by_entry(payload)

        assert planned["memory"]["action"] == "remove"
        assert planned["memory"]["reasons"] == ["entry_removed"]
        assert planned["memory"]["generation_impact"] == "new_generation"
        assert payload["would_publish"] is True

        harness.apply_config(remaining)
        result = await harness.reconcile()

        assert "memory" in result.disposed
        assert set(result.reused) == {"orders-db"}
    finally:
        await harness.stop()


async def test_preview_of_the_current_state_matches_an_incremental_install() -> None:
    harness = catalogued()
    harness.apply_config(INITIAL)
    await harness.start()
    try:
        harness.install(cache_provider, entry_id="warm-cache")

        payload = harness.preview().to_dict()
        planned = actions_by_entry(payload)
        assert planned["warm-cache"]["action"] == "add"
        assert payload["would_publish"] is True

        result = await harness.reconcile()

        assert "warm-cache" in result.mounted
        assert set(result.reused) == {"orders-db", "memory"}
    finally:
        await harness.stop()
