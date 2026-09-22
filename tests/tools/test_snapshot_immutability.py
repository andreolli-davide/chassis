"""A published tool snapshot never aliases the live registry (R009/G4).

Publication is a deep-copy boundary: once a generation holds a tool's
registration, mutating the live entry -- rebinding its policy or reaching into
nested collections -- must change neither the acquired generation's policy nor
its exported representation. The live registry stays mutable for pre-publish
registration work; published copies are immutable.
"""

from __future__ import annotations

import pytest
from langchain_core.tools import tool

from chassis import Harness, PluginContext, plugin
from chassis.core.scope import Scope
from chassis.tools import ToolPolicy, ToolRegistry


@tool
def shred(path: str) -> str:
    """Delete a file at ``path``."""

    return path


def test_a_snapshot_exposes_detached_copies_of_the_live_registrations() -> None:
    registry = ToolRegistry()
    scope = Scope("owner")
    registry.register(
        scope=scope,
        tool=shred,
        policy=ToolPolicy(permissions=("filesystem.delete",), metadata={"note": {"deep": 1}}),
        owner_id="plugin_1",
        metadata={"note": {"deep": 1}},
    )

    snapshot = registry.snapshot("gen_0001")
    entry = snapshot.get("shred")
    assert entry is not None
    assert entry.policy.permissions == ("filesystem.delete",)
    before = entry.to_dict()
    snapshot_before = snapshot.to_dict()

    live = registry.get("shred")
    assert live is not None

    # Nested collections reachable through the live policy are frozen copies.
    with pytest.raises(TypeError):
        live.policy.metadata["note"] = {"deep": 9}  # type: ignore[index]
    with pytest.raises(TypeError):
        live.policy.metadata["note"]["deep"] = 9  # type: ignore[index]
    with pytest.raises(TypeError):
        live.metadata["note"]["deep"] = 9  # type: ignore[index]

    # The live registration stays mutable after publication ...
    live.policy = ToolPolicy(permissions=())

    # ... but the published copy is unchanged and detached from the registry.
    assert entry.policy.permissions == ("filesystem.delete",)
    assert entry.policy.metadata["note"]["deep"] == 1  # type: ignore[index]
    assert entry.to_dict() == before
    assert snapshot.to_dict() == snapshot_before
    assert snapshot.get("shred") is not live

    # And the published copy itself rejects mutation.
    with pytest.raises(TypeError):
        entry.policy = ToolPolicy()


async def test_post_publication_mutation_never_alters_an_acquired_generation() -> None:
    @plugin(name="danger", version="1.0.0")
    async def danger(ctx: PluginContext) -> None:
        ctx.tools.register(
            shred,
            policy=ToolPolicy(
                permissions=("filesystem.delete",),
                side_effects=("destructive",),
                metadata={"note": {"deep": 1}},
            ),
        )

    harness = Harness()
    harness.install(danger, entry_id="danger")
    await harness.start()
    try:
        generation = harness.current_generation
        assert generation is not None

        async with harness.acquire() as acquired:
            snapshot = harness.tool_snapshot(acquired)
            entry = snapshot.get("shred")
            assert entry is not None
            assert entry.policy.permissions == ("filesystem.delete",)
            before = snapshot.to_dict()

            live = harness.tools.get("shred")
            assert live is not None
            live.policy = ToolPolicy(permissions=())

            assert entry.policy.permissions == ("filesystem.delete",)
            assert entry.policy.metadata["note"]["deep"] == 1  # type: ignore[index]
            assert snapshot.to_dict() == before

            fresh = harness.tool_snapshot(acquired)
            fresh_entry = fresh.get("shred")
            assert fresh_entry is not None
            assert fresh_entry.policy.permissions == ("filesystem.delete",)
            assert fresh.to_dict() == before
            assert fresh_entry is not live
    finally:
        await harness.stop()
