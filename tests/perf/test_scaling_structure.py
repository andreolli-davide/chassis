"""Deterministic structural scaling tests (roadmap R032).

These count the work an operation performs instead of timing it, so they are
stable on shared runners and gate CI where wall-clock benchmarks cannot. The
claim under test: the control-plane hot paths do a bounded amount of work per
entry — one semantic-identity computation per entry per plan/reconcile, one
registry scan per tool lookup, and zero composition work on the data plane.
"""

from __future__ import annotations

from typing import Any

import pytest

from chassis import DATABASE, Harness, PluginContext, plugin
from chassis.testing import TestHarness, fake_tool


@plugin(name="perf-db", version="1.0.0", provides={"database": "1.0.0"})
async def db_plugin(ctx: PluginContext) -> None:
    ctx.capabilities.provide(DATABASE, f"{ctx.entry_id}-handle")


def count_identity(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Wrap the semantic-identity builder with a call counter."""

    import chassis.harness as harness_module

    calls: list[int] = []
    original = harness_module.build_semantic_identity

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(harness_module, "build_semantic_identity", counting)
    return calls


def install_entries(harness: Harness, count: int) -> None:
    for index in range(count):
        harness.install(db_plugin, entry_id=f"db-{index}", config={"pool": index})


async def test_reconcile_computes_exactly_one_identity_per_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for count in (20, 100):
        harness = Harness()
        install_entries(harness, count)
        calls = count_identity(monkeypatch)

        await harness.start()
        first = len(calls)
        await harness.reconcile()
        second = len(calls) - first

        # One identity computation per entry per reconcile: a quadratic
        # implementation would grow with count squared.
        assert first == count, f"reconcile of {count} entries computed {first} identities"
        assert second == count, f"no-op reconcile of {count} entries computed {second}"
        await harness.stop()


async def test_preview_computes_exactly_one_identity_per_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for count in (20, 100):
        harness = Harness()
        install_entries(harness, count)
        calls = count_identity(monkeypatch)

        harness.preview()
        assert len(calls) == count, f"preview of {count} entries computed {len(calls)} identities"


async def test_tool_snapshot_lookup_is_one_registry_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import chassis.tools.registry as registry_module

    scans: list[int] = []
    original = registry_module.ToolRegistry.snapshot

    def counting(self: Any, *args: Any, **kwargs: Any) -> Any:
        scans.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(registry_module.ToolRegistry, "snapshot", counting)

    async with TestHarness() as harness:
        harness.install_tools(
            *(
                fake_tool(f"perf-tool-{index}", result="ok", parameters={"x": (str, ...)})
                for index in range(50)
            )
        )
        await harness.reconcile()
        generation = harness.current_generation
        assert generation is not None
        scans.clear()

        for _ in range(5):
            harness.tool_snapshot(generation)

        assert len(scans) == 5, "one registry scan per lookup, independent of tool count"


async def test_acquire_and_release_do_no_composition_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness()
    install_entries(harness, 20)
    await harness.start()
    try:
        calls = count_identity(monkeypatch)

        for _ in range(50):
            async with harness.acquire():
                pass

        assert calls == [], "the data plane must not recompute composition per run"
    finally:
        await harness.stop()
