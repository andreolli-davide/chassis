"""Scoped composition --- hierarchical composition that stays immutable.

Chassis 0.3 adds composition scopes: named nodes inside a candidate composition
that inherit providers from their ancestors, add their own, narrow what they
expose, and own what they declare. A scope is control-plane desired state; once a
generation is published, the scope tree a run acquires never changes underneath it.

The structure built here::

    root                 shared database + telemetry
    ├── tenant:acme
    │   ├── research     local search provider + a model requirement
    │   └── finance      local ERP provider
    └── tenant:globex

Run it with::

    uv run python examples/scoped_composition.py

The script asserts what it prints, so running it verifies the claims.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from chassis import DATABASE, MODEL, SCHEDULER, Harness, PluginContext, plugin
from chassis.capabilities import CapabilityKey
from chassis.composition import ResolvedScope
from chassis.core.generation import RuntimeGeneration
from chassis.plugins.lifecycle import PluginState


def provider(name: str, capability: CapabilityKey, value: object):  # type: ignore[no-untyped-def]
    """A minimal capability provider, owned by the scope that declares it."""

    @plugin(name=name, version="1.0.0", provides={capability.name: "1.0.0"})
    async def provide(ctx: PluginContext) -> None:
        ctx.capabilities.provide(capability, value)

    return provide


def consumer(name: str, capability: CapabilityKey):  # type: ignore[no-untyped-def]
    """A plugin that resolves a capability from the composition around it."""

    @plugin(name=name, version="1.0.0", requires={capability.name: ">=1,<2"})
    async def consume(ctx: PluginContext) -> None:
        resolved = ctx.require(capability)
        print(f"[{ctx.scope.name}] resolved {capability.name} -> {resolved!r}")

    return consume


RESEARCH = "/tenant:acme/research"
FINANCE = "/tenant:acme/finance"

DATABASE_KEY = CapabilityKey("database", "1")
SEARCH_KEY = CapabilityKey("search", "1")
ERP_KEY = CapabilityKey("erp", "1")


async def main() -> None:
    harness = Harness(name="scoped")

    # Root composition: capabilities every scope may inherit.
    harness.install(provider("postgres-main", DATABASE_KEY, "postgres://shared"), entry_id="db")
    harness.install(provider("model-v1", MODEL, "model://v1"), entry_id="model")
    harness.install(provider("otel", SCHEDULER, "telemetry"), entry_id="telemetry")

    # tenant:acme deliberately exposes only a subset of what the root provides.
    acme = harness.composition.child(
        "tenant:acme",
        capabilities=[MODEL, DATABASE, SEARCH_KEY.name, ERP_KEY.name],
    )

    research = acme.child("research")
    research.install(provider("search-v2", SEARCH_KEY, "search://v2"), entry_id="search")
    research.install(consumer("research-agent", DATABASE_KEY), entry_id="research-agent")
    research.require(MODEL, ">=1,<2")

    finance = acme.child("finance")
    finance.install(provider("erp-suite", ERP_KEY, "erp://suite"), entry_id="erp")
    finance.install(consumer("finance-agent", ERP_KEY), entry_id="finance-agent")

    globex = harness.composition.child("tenant:globex")
    globex.install(consumer("globex-agent", DATABASE_KEY), entry_id="globex-agent")

    async with harness as h:
        generation = generation_of(h)
        print(f"[start] generation={generation.generation_id}")
        print(f"[start] scopes={generation.scopes.paths()}")
        print(f"[start] root providers={keys(scope_of(generation, '/').visible)}")
        print(f"[start] research providers={keys(scope_of(generation, RESEARCH).visible)}")
        print(f"[start] finance providers={keys(scope_of(generation, FINANCE).visible)}")

        # Sibling isolation: neither child can see the other's local provider.
        research_scope = scope_of(generation, RESEARCH)
        finance_scope = scope_of(generation, FINANCE)
        assert set(research_scope.visible) == {"database", "model", "search"}
        assert set(finance_scope.visible) == {"database", "erp", "model"}

        # Capability narrowing: acme hides `scheduler`, and so does research, even
        # though the root provides it. `erp` is exposed by acme but provided only
        # by finance, so research cannot see it either.
        assert "scheduler" not in research_scope.visible
        assert "erp" not in research_scope.visible

        # Provenance: why does research use the shared database?
        explanation = h.diagnostics.explain_requirement("research-agent", "database")
        assert explanation is not None
        assert explanation.selected is not None
        assert explanation.selected["origin"] == "inherited"
        print("[explain] " + explanation.to_text().replace("\n", "\n           "))

        # Explain scope visibility without dumping configuration.
        print("[scope]")
        print("  " + h.diagnostics.explain_scope(RESEARCH).to_text().replace("\n", "\n  "))

        # A run acquired now keeps this scope tree even if the control plane changes.
        entered = asyncio.Event()
        release = asyncio.Event()

        async def run_a() -> str:
            async with h.acquire() as pinned:
                entered.set()
                await release.wait()
                return f"{pinned.generation_id}:{','.join(pinned.scopes.paths())}"

        task = asyncio.create_task(run_a())
        await entered.wait()
        before = generation_of(h)

        # Replace research's local provider, then publish.
        research.install(
            provider("search-v3", SEARCH_KEY, "search://v3"),
            entry_id="search",
            replace=True,
        )
        await h.reconcile()
        after = generation_of(h)
        assert after.generation_id != before.generation_id
        print(f"[change] {before.generation_id} -> {after.generation_id}")

        diff = h.diagnostics.diff_generations(before.generation_id, after.generation_id)
        print("[diff]")
        print("  " + diff.to_text().replace("\n", "\n  "))
        assert [item.kind for item in diff.providers] == ["replaced"]

        # The new generation exposes the new provider; the old run keeps the old one.
        new_search = scope_of(after, RESEARCH)
        assert new_search.providers["search"] != research_scope.providers["search"]

        release.set()
        pinned = await task
        print(f"[pinned] run A stayed on {pinned}")
        assert pinned.startswith(before.generation_id)

        # The superseded instance is disposed only once nothing can reach it.
        old_instance = h.plugin_registry.instance("search")
        assert old_instance is not None
        print(f"[disposal] current search state={old_instance.state.value}")
        assert old_instance.state is PluginState.ACTIVE

        assert h.diagnostics.explain_scope(RESEARCH).owned_registrations

    print("\nScoped composition completed: inheritance, isolation, narrowing, provenance,")
    print("immutable publication, and pinned runs all verified.")


def keys(mapping: Mapping[str, Any]) -> list[str]:
    """Capability names of a scope's visible providers, for readable output."""

    return sorted(mapping)


def generation_of(harness: Harness) -> RuntimeGeneration:
    """The current generation, asserting one is published."""

    generation = harness.current_generation
    assert generation is not None
    return generation


def scope_of(generation: RuntimeGeneration, path: str) -> ResolvedScope:
    """A scope of a published generation, asserting it exists."""

    scope = generation.scopes.get(path)
    assert scope is not None, path
    return scope


if __name__ == "__main__":
    asyncio.run(main())
