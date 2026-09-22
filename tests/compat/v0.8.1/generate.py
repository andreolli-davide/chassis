"""Regenerate the 0.8.1 fixtures with the released 0.8.1 implementation.

Provenance rules (see ``README.md`` here): fixtures must come from the released
0.8.1 code — the ``v0.8.1`` tag or an installed 0.8.1 distribution — never from
the current tree. Run this file with the *0.8.1* interpreter::

    git worktree add --detach /tmp/chassis-081 v0.8.1
    uv venv /tmp/venv081
    uv pip install --python /tmp/venv081/bin/python "/tmp/chassis-081[langgraph,langsmith]"
    /tmp/venv081/bin/python tests/compat/v0.8.1/generate.py

Generation drives a real 0.8.1 harness (real plugin setup, real tool execution
through the executor's recording boundary, a real replay-wrapped model call,
real lifecycle and snapshot records). Values are sanitized afterwards: fixed
timestamps, deterministic stand-ins for runtime instance ids, call ids, and run
ids. No secrets, paths, or machine-specific data are written.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

from chassis import DATABASE, MEMORY, PluginContext, plugin
from chassis.config import parse_config
from chassis.replay import ReplayChatModel, ReplayMode, ReplaySession
from chassis.testing import FakeChatModel, TestHarness, fake_tool
from chassis.tools import ToolRequest

HERE = Path(__file__).resolve().parent
CREATED_AT = 1_760_000_000.0
FIXED_DURATION_SECONDS = 0.25

_INSTANCE_ID = re.compile(r"^plugin_[0-9a-f]+$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_LEFTOVER_INSTANCE_ID = re.compile(r'"plugin_[0-9a-f]+"')
_LEFTOVER_UUID = re.compile(r'"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"')
_VOLATILE_FIELDS = {"created_at": CREATED_AT, "duration_seconds": FIXED_DURATION_SECONDS}
_IDENTITY_FIELDS = {"run_id": "run_fixture_1", "tool_call_id": "call_fixture_1"}

CONFIGURATION = """\
version: 1
plugins:
  - id: model
    plugin: support-model
    config:
      model: example-model
      api_key: placeholder-not-a-secret
  - id: orders
    plugin: order-lookup
provider_preferences:
  database: orders-db
"""


@plugin(name="orders-db", version="1.0.0", provides={"database": "1.0.0"})
async def orders_db(ctx: PluginContext) -> None:
    ctx.capabilities.provide(DATABASE, "orders-db-handle")


@plugin(
    name="memory", version="2.1.0", provides={"memory": "1.0.0"}, requires={"database": ">=1,<2"}
)
async def memory_plugin(ctx: PluginContext) -> None:
    ctx.require("database")
    ctx.capabilities.provide(MEMORY, "memory-handle")


def _scrub(value: Any, rename: dict[str, str], uuids: dict[str, str]) -> Any:
    """Deterministic stand-ins for volatile identity and timing, recursively."""

    if isinstance(value, str):
        if _INSTANCE_ID.match(value):
            return rename.setdefault(value, f"plugin_fixture_{len(rename)}")
        if _UUID.match(value):
            return uuids.setdefault(value, f"id_fixture_{len(uuids)}")
        return value
    if isinstance(value, dict):
        scrubbed: dict[str, Any] = {}
        for key, item in value.items():
            if key in _VOLATILE_FIELDS:
                scrubbed[key] = _VOLATILE_FIELDS[key]
            elif key in _IDENTITY_FIELDS and item is not None:
                scrubbed[key] = _IDENTITY_FIELDS[key]
            else:
                scrubbed[key] = _scrub(item, rename, uuids)
        return scrubbed
    if isinstance(value, list):
        return [_scrub(item, rename, uuids) for item in value]
    return value


def _sanitize(payload: dict[str, Any]) -> dict[str, Any]:
    """Replace volatile identity and timing values; verify nothing remains."""

    document = _scrub(payload, {}, {})
    rendered = json.dumps(document, sort_keys=True)
    assert not _LEFTOVER_INSTANCE_ID.search(rendered), rendered[:200]
    assert not _LEFTOVER_UUID.search(rendered), rendered[:200]
    return document


async def _build_snapshot() -> dict[str, Any]:
    document: dict[str, Any] = {}
    async with TestHarness() as harness:
        harness.install(orders_db, entry_id="orders-db")
        harness.install(memory_plugin, entry_id="memory")
        harness.prefer_provider("database", "orders-db")
        await harness.start()
        generation = harness.current_generation
        assert generation is not None
        snapshot = harness.snapshot_for(
            generation, agent="support-agent", metadata={"dataset": "compat-fixtures"}
        )
        document = _sanitize(snapshot.to_dict())
    return document


async def _build_recording() -> dict[str, Any]:
    recording = ReplaySession(
        mode=ReplayMode.RECORD,
        metadata={"dataset": "compat-fixtures", "produced_by": "chassis-harness 0.8.1"},
    )
    async with TestHarness(replay=recording) as harness:
        harness.install(orders_db, entry_id="orders-db")
        harness.install_tools(
            fake_tool(
                "lookup_order",
                result={"order": "A-1", "status": "shipped"},
                parameters={"order": (str, ...)},
            )
        )
        await harness.reconcile()
        generation = harness.current_generation
        assert generation is not None

        result = await harness.tool_executor.execute(
            ToolRequest(name="lookup_order", args={"order": "A-1"}),
            snapshot=harness.tool_snapshot(generation),
        )
        assert result is not None and result.content == {"order": "A-1", "status": "shipped"}

        model = ReplayChatModel(
            session=recording,
            inner=FakeChatModel(responses=["Order A-1 shipped."]),
            model_name="fake-model",
        )
        await model.ainvoke([HumanMessage("where is order A-1?")])

    document = _sanitize(recording.to_dict())
    kinds = {entry["kind"] for entry in document["records"]}
    assert kinds >= {"tool", "model", "snapshot", "lifecycle"}, kinds
    return document


def _write_configuration() -> None:
    config = parse_config(CONFIGURATION)
    assert config.version == 1
    (HERE / "configuration.yaml").write_text(CONFIGURATION, encoding="utf-8")


def main() -> None:
    snapshot = asyncio.run(_build_snapshot())
    recording = asyncio.run(_build_recording())
    _write_configuration()

    (HERE / "runtime-snapshot.json").write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (HERE / "replay-recording.json").write_text(
        json.dumps(recording, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    for name in ("runtime-snapshot.json", "replay-recording.json", "configuration.yaml"):
        content = (HERE / name).read_bytes()
        print(f"{hashlib.sha256(content).hexdigest()}  {name}")


if __name__ == "__main__":
    main()
