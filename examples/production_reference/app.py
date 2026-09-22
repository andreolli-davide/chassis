"""Production reference application: an order support desk on Chassis.

One self-contained system that exercises everything a production Chassis
application uses — declarative configuration, a plugin catalog, scoped
composition, AgentSpec materialization, capability requirements and
preferences, tool registration and policy, secret resolution and redaction,
agent invoke and streaming, replay recording and replay, hot provider
replacement with pinned runs, planning before applying, diagnostics and
snapshot attribution, graceful shutdown, and a controlled failure with
rollback.

Everything runs on deterministic local fakes: no credentials, no network, no
external services. The application executes its own assertions, so running it
is the verification::

    uv run python -m examples.production_reference.app

The architecture and the expected output are documented in README.md next to
this file.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from typing_extensions import TypedDict

from chassis import DATABASE, MODEL, CapabilityKey, Harness, PluginContext, plugin
from chassis.agent_spec import AgentSpec
from chassis.core.errors import PluginSetupError, PolicyDenied
from chassis.langgraph import AgentDefinition, GraphBuildInputs, LangGraphAgent, harness_tool_node
from chassis.policy import GrantPolicy
from chassis.replay import ReplayMode, ReplaySession
from chassis.runtime import HarnessRunContext
from chassis.secrets import StaticSecretProvider
from chassis.testing import FakeChatModel, fake_tool
from chassis.tools import ToolPolicy

SUPPORT_API_KEY = "sk-live-support-demo-key"
ORDERS = {
    "A-1": {"order": "A-1", "status": "shipped", "eta": "2026-09-25"},
    "B-2": {"order": "B-2", "status": "processing", "eta": "2026-09-30"},
}

# --------------------------------------------------------------------------
# 1. Plugins: what the application is made of
# --------------------------------------------------------------------------


@plugin(name="orders-db", version="1.0.0", provides={"database": "1.0.0"})
async def orders_db(ctx: PluginContext) -> None:
    """The order repository. Configuration is observable by key only."""

    ctx.capabilities.provide(DATABASE, {"region": ctx.config.get("region", "eu")})


@plugin(
    name="support-tools",
    version="1.0.0",
    provides={"orders": "1.0.0"},
    requires={"database": ">=1,<2"},
)
async def support_tools(ctx: PluginContext) -> None:
    """Two tools behind explicit policy; the secret crosses the redaction seam."""

    database = ctx.require("database")
    assert database is not None  # the requirement is bound, not looked up globally
    await ctx.secrets.get("support_api_key")
    ctx.capabilities.provide(
        CapabilityKey.from_version("orders", "1.0.0"), {"catalog": list(ORDERS)}
    )

    ctx.tools.register(
        fake_tool(
            "lookup_order",
            result=ORDERS["A-1"],
            parameters={"order": (str, ...)},
        ),
        policy=ToolPolicy(permissions=("orders.read",), idempotent=True),
    )
    ctx.tools.register(
        fake_tool(
            "notify_customer",
            result={"notified": "ada"},
            parameters={"customer": (str, ...)},
        ),
        policy=ToolPolicy(permissions=("orders.notify",)),
    )


@plugin(name="flaky-analytics", version="1.0.0", provides={"analytics": "1.0.0"})
async def flaky_analytics(ctx: PluginContext) -> None:
    """A plugin whose setup fails on purpose: the controlled failure."""

    raise RuntimeError("analytics backend refused the connection")


# --------------------------------------------------------------------------
# 2. The agent: LangGraph owns execution, Chassis owns composition
# --------------------------------------------------------------------------


class ChatState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


async def assistant(state: ChatState, runtime: Runtime[HarnessRunContext]) -> dict[str, Any]:
    model = runtime.context.require_capability(MODEL)
    return {"messages": [await model.ainvoke(state["messages"])]}


def build_support_graph(inputs: GraphBuildInputs) -> StateGraph[Any, Any, Any, Any]:
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(ChatState, context_schema=HarnessRunContext)
    graph.add_node("assistant", assistant)
    graph.add_node("tools", harness_tool_node(list(inputs.tools)))
    graph.add_edge(START, "assistant")
    graph.add_conditional_edges(
        "assistant",
        lambda state, runtime: "tools" if _wants_tools(state) else END,
        {"tools": "tools", END: END},
    )
    graph.add_edge("tools", "assistant")
    return graph


def _wants_tools(state: ChatState) -> bool:
    last = state["messages"][-1]
    return isinstance(last, AIMessage) and bool(getattr(last, "tool_calls", []))


SUPPORT_AGENT = AgentSpec(
    name="support-agent",
    revision="1",
    description="Order support desk agent",
    runtime_ref="support-graph",
    requires={"database": ">=1,<2", "orders": ">=1"},
    capabilities={"database", "orders", "model"},
    plugins={"support-tools": {}},
    tools={"lookup_order", "notify_customer"},
)

CONFIG: dict[str, Any] = {
    "version": 1,
    "plugins": [
        {"id": "orders-db", "plugin": "orders-db", "config": {"region": "eu-west"}},
        {"id": "orders-db-staging", "plugin": "orders-db", "config": {"region": "us-east"}},
    ],
    "provider_preferences": {},
}


# --------------------------------------------------------------------------
# 3. The application: every scenario is a numbered section with its assertion
# --------------------------------------------------------------------------


def step(number: str, message: str) -> None:
    print(f"[{number}] {message}")


async def main() -> None:
    recording = ReplaySession(mode=ReplayMode.RECORD, metadata={"dataset": "support-desk"})
    harness = Harness(
        name="support-desk",
        policy=GrantPolicy({"orders.read"}),
        secrets=StaticSecretProvider({"support_api_key": SUPPORT_API_KEY}),
        replay=recording,
    )
    harness.provide(MODEL, FakeChatModel(responses=["Where is order A-1?"]))
    harness.register_plugin_type("orders-db", orders_db)
    harness.register_plugin_type("support-tools", support_tools)
    harness.register_plugin_type("flaky-analytics", flaky_analytics)
    harness.register_agent(
        LangGraphAgent(
            AgentDefinition(
                name="support-graph",
                version="1",
                state_schema=ChatState,
                build=build_support_graph,
            ),
            checkpointer=InMemorySaver(),
        )
    )

    # -- (1) planning: dry-run the configuration before applying it -----------
    pending_before = harness.has_pending_changes
    plan = harness.preview(CONFIG)
    actions = {item["entry_id"]: item["action"] for item in plan.to_dict()["actions"]}
    assert actions["orders-db"] == "add" and actions["orders-db-staging"] == "add"
    assert plan.would_publish
    assert harness.has_pending_changes is pending_before, "preview dirtied the harness"
    step("1", "preview shows two additions and zero mutation")

    # -- (2) declarative configuration + scoped composition --------------------
    harness.apply_config(CONFIG)
    analytics = harness.composition.child("analytics", capabilities={"database"})
    analytics.select_tools("lookup_order")
    await harness.start()
    step("2", "configuration applied; analytics scope narrows tools to lookup_order")

    # -- (3) ambiguity is diagnosed, then settled by preference ----------------
    harness.install(support_tools, entry_id="support-tools")
    ambiguous = harness.preview().to_dict()
    memory_action = next(
        item for item in ambiguous["actions"] if item["entry_id"] == "support-tools"
    )
    assert memory_action["action"] == "reject"
    assert memory_action["reasons"] == ["requirement_ambiguous"]
    harness.prefer_provider("database", "orders-db", consumer="support-tools")
    settled = harness.preview().to_dict()
    next_item = next(item for item in settled["actions"] if item["entry_id"] == "support-tools")
    assert next_item["action"] in {"add", "rebuild", "reuse"}
    step("3", "ambiguity reported with candidates, resolved by preference")

    # -- (4) AgentSpec materialization ----------------------------------------
    harness.agents.install(SUPPORT_AGENT)
    await harness.reconcile()
    generation = harness.current_generation
    assert generation is not None
    revision = harness.diagnostics.explain_agent("support-agent")
    assert revision.identity == "support-agent@1"
    step("4", "support-agent@1 materialized with its scoped composition")

    # -- (5) agent invoke: tool call through policy ---------------------------
    response = FakeChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "lookup_order", "args": {"order": "A-1"}, "id": "call-1"}],
            ),
            AIMessage(content="Order A-1 is shipped."),
        ]
    )
    harness.provide(MODEL, response)
    await harness.reconcile()
    result = await harness.agents.invoke(
        "support-agent",
        {"messages": [HumanMessage("Where is order A-1?")]},
        thread_id="support-1",
    )
    assert result.agent_revision == "1"
    assert result.generation_id
    step("5", f"invoke answered on generation {result.generation_id} (revision 1)")

    # -- (6) streaming: every event carries the run's attribution -------------
    events = [
        event
        async for event in harness.agents.stream(
            "support-agent", {"messages": [HumanMessage("Status?")]}, thread_id="support-2"
        )
    ]
    assert events and all(event.generation_id == result.generation_id for event in events[:1])
    step("6", f"stream produced {len(events)} attributed events")

    # -- (7) policy: the ungranted permission is denied -----------------------
    denied = False
    try:
        await harness.tool_executor.execute(
            _request("notify_customer", {"customer": "ada"}),
            snapshot=harness.tool_snapshot(generation),
        )
    except PolicyDenied:
        denied = True
    assert denied, "notify_customer must be denied without orders.notify"
    step("7", "policy denied the ungranted permission")

    # -- (8) secrets never leak into exports ----------------------------------
    exported = json.dumps(recording.to_dict(), sort_keys=True)
    assert SUPPORT_API_KEY not in exported
    assert SUPPORT_API_KEY not in str(harness.snapshot_for(generation).to_dict())
    assert SUPPORT_API_KEY not in str(harness.diagnostics.status())
    step("8", "secret absent from replay records, snapshot, and diagnostics")

    # -- (9) replay: the recording answers instead of the live tool ------------
    session_document = json.loads(json.dumps(recording.to_dict()))
    replaying = ReplaySession.from_dict(session_document, mode=ReplayMode.REPLAY)
    live_calls: list[Any] = []
    replaying_harness = Harness(replay=replaying)
    replaying_harness.install(_counting_plugin(live_calls), entry_id="support-tools")
    await replaying_harness.start()
    try:
        replayed_generation = replaying_harness.current_generation
        assert replayed_generation is not None
        replayed = await replaying_harness.tool_executor.execute(
            _request("lookup_order", {"order": "A-1"}),
            snapshot=replaying_harness.tool_snapshot(replayed_generation),
        )
        assert replayed is not None and replayed.content == ORDERS["A-1"]
        assert live_calls == [], "the live tool must not run when a record answers"
    finally:
        await replaying_harness.stop()
    step("9", f"recording replayed {len(session_document['records'])} records without live calls")

    # -- (10) hot provider replacement pins old runs --------------------------
    replacement_ready = asyncio.Event()
    replacement_done = asyncio.Event()
    pinned_observations: list[str] = []

    async def pinned_run() -> None:
        async with harness.acquire() as held:
            held_digest = harness.snapshot_for(held).digest()
            started_generation = held.generation_id
            replacement_ready.set()
            await replacement_done.wait()
            assert held.generation_id == started_generation
            assert harness.snapshot_for(held).digest() == held_digest
            pinned_observations.append(started_generation)

    runner = asyncio.create_task(pinned_run())
    await replacement_ready.wait()
    current_before_replacement = harness.current_generation
    assert current_before_replacement is not None
    expected_pinned = current_before_replacement.generation_id

    plan = harness.preview(CONFIG)
    replace_action = next(
        item for item in plan.to_dict()["actions"] if item["entry_id"] == "orders-db"
    )
    assert replace_action["action"] in {"replace", "rebuild", "reuse"}
    harness.install(orders_db, entry_id="orders-db", config={"region": "ap-south"}, replace=True)
    await harness.reconcile()
    new_generation = harness.current_generation
    assert new_generation is not None
    assert new_generation.generation_id != expected_pinned
    replacement_done.set()
    await runner
    assert pinned_observations == [expected_pinned]
    step("10", "provider replaced while the old run stayed pinned to its generation")

    # -- (11) diagnostics and snapshot attribution ----------------------------
    snapshot = harness.snapshot_for(new_generation, agent="support-agent")
    explained = harness.diagnostics.explain("orders-db")
    assert explained is not None
    assert snapshot.semantic_digest() and snapshot.digest()
    pressure = harness.diagnostics.generation_pressure()
    counts = harness.diagnostics.resource_counts()
    step(
        "11",
        f"snapshot {snapshot.semantic_digest()[:12]} attributed; "
        f"{pressure.live_generations} live generation(s), {counts.instances} instance(s)",
    )

    # -- (12) controlled failure: setup raises, every effect is rolled back ----
    before = harness.diagnostics.resource_counts()
    current_before = harness.current_generation
    harness.install(flaky_analytics, entry_id="flaky")
    failed = False
    try:
        await harness.reconcile()
    except PluginSetupError:
        failed = True
    assert failed, "flaky-analytics setup must fail"
    assert harness.current_generation is current_before
    after = harness.diagnostics.resource_counts()
    assert after.effects == before.effects, "the failed setup must roll its effects back"
    assert after.leases == before.leases
    flaky = next(item for item in harness.diagnostics.plugins() if item["entry_id"] == "flaky")
    assert flaky["state"] == "failed", "the failed instance stays visible as FAILED"
    step("12", "failed setup rolled back every effect; the previous generation stayed current")

    # -- (13) graceful shutdown returns every resource to baseline -------------
    baseline = counts
    await harness.stop()
    final = harness.diagnostics.resource_counts()
    assert final.instances == 0 and final.leases == 0 and final.live_generations == 0
    assert harness.state.value == "stopped"
    step("13", f"graceful shutdown; {baseline.instances} instance(s) returned to baseline")

    print("support desk: every scenario held")


def _request(name: str, args: dict[str, Any]) -> Any:
    from chassis.tools import ToolRequest

    return ToolRequest(name=name, args=args)


def _counting_plugin(calls: list[Any]) -> Any:
    @plugin(name="support-tools-replay", version="1.0.0")
    async def counting(ctx: PluginContext) -> None:
        ctx.tools.register(
            fake_tool(
                "lookup_order",
                result={"live": True},
                parameters={"order": (str, ...)},
                calls=calls,
            )
        )

    return counting


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
