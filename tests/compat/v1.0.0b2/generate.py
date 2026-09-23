"""Generate the deterministic persisted-format candidates for 1.0.0b2.

Run from the repository root with the 1.0.0b2 environment installed::

    uv run python tests/compat/v1.0.0b2/generate.py

CI uses ``--check`` to regenerate the documents in memory and fail when a
fixture no longer describes what the implementation emits.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import chassis
from chassis import DATABASE, MEMORY, Harness, PluginContext, plugin
from chassis.config import parse_config
from chassis.replay import BoundaryKind, ReplayMode, ReplaySession, boundary_key

HERE = Path(__file__).resolve().parent
PACKAGE_VERSION = "1.0.0b2"
FIXED_TIME = 1_790_000_000.0
FIXED_AGE_SECONDS = 0.25

_INSTANCE_ID = re.compile(r"^plugin_[0-9a-f]+$")
_LEFTOVER_INSTANCE_ID = re.compile(r'"plugin_[0-9a-f]+"')
_TIME_FIELDS = {"created_at": FIXED_TIME, "generated_at": FIXED_TIME}

CONFIGURATION = """\
version: 1
plugins:
  - id: orders-db
    plugin: orders-db
    config:
      endpoint: https://example.invalid/orders
  - id: memory
    plugin: memory
    config:
      max_items: 128
provider_preferences:
  database: orders-db
"""


@plugin(name="orders-db", version="1.0.0", provides={"database": "1.0.0"})
async def orders_db(ctx: PluginContext) -> None:
    ctx.capabilities.provide(DATABASE, "orders-db-handle")


@plugin(
    name="memory",
    version="2.1.0",
    provides={"memory": "1.0.0"},
    requires={"database": ">=1,<2"},
)
async def memory(ctx: PluginContext) -> None:
    ctx.require("database")
    ctx.capabilities.provide(MEMORY, "memory-handle")


def _rename_map(harness: Harness) -> dict[str, str]:
    pairing = {
        instance.instance_id: instance.entry_id for instance in harness.plugin_registry.instances()
    }
    return {
        instance_id: f"plugin_fixture_{index}"
        for index, instance_id in enumerate(sorted(pairing, key=lambda item: pairing[item]))
    }


def _scrub(value: Any, rename: dict[str, str], *, field: str | None = None) -> Any:
    """Replace runtime identity and time without changing semantic content."""

    if field in _TIME_FIELDS:
        return _TIME_FIELDS[field]
    if field == "age_seconds":
        return FIXED_AGE_SECONDS
    if isinstance(value, str):
        return rename.get(value, value)
    if isinstance(value, dict):
        return {
            rename.get(str(key), str(key)): _scrub(item, rename, field=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item, rename) for item in value]
    return value


def _json_bytes(document: dict[str, Any]) -> bytes:
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    assert not _LEFTOVER_INSTANCE_ID.search(rendered), rendered[:300]
    assert "/Users/" not in rendered
    assert "/home/" not in rendered
    return rendered.encode()


def _canonicalize_pressure(document: dict[str, Any]) -> dict[str, Any]:
    """Order set-derived diagnostic collections by their semantic identity."""

    document["generations"].sort(key=lambda item: item["generation_id"])
    for generation in document["generations"]:
        generation["retained_plugins"].sort(key=lambda item: item["entry_id"])
    document["resources"].sort(key=lambda item: item["entry_id"])
    return document


async def _runtime_documents() -> dict[str, dict[str, Any]]:
    harness = Harness(name="format-fixture")
    harness.register_plugin_type("orders-db", orders_db)
    harness.register_plugin_type("memory", memory)

    applied = harness.apply_config(CONFIGURATION)
    planning = harness.preview()
    reconciled = await harness.start()
    try:
        generation = harness.current_generation
        assert generation is not None
        rename = _rename_map(harness)

        snapshot = _scrub(
            harness.snapshot_for(
                generation,
                agent="support-agent",
                metadata={"dataset": "compat-fixtures"},
            ).to_dict(),
            rename,
        )
        pressure = _canonicalize_pressure(
            _scrub(harness.diagnostics.generation_pressure().to_dict(), rename)
        )

        recording = ReplaySession(
            mode=ReplayMode.RECORD,
            metadata={
                "dataset": "compat-fixtures",
                "produced_by": f"chassis-harness {PACKAGE_VERSION}",
            },
        )
        recording.record(
            BoundaryKind.TOOL,
            key=boundary_key("tool", "lookup_order", {"order": "A-1"}),
            request={"tool": "lookup_order", "args": {"order": "A-1"}},
            response={
                "name": "lookup_order",
                "content": {"order": "A-1", "status": "shipped"},
                "artifact": None,
                "error": None,
                "status": "ok",
                "redacted": False,
            },
            generation_id=generation.generation_id,
            run_id="run_fixture_1",
        )
        recording.record_snapshot(snapshot)
        recording.record_lifecycle(
            "generation.publish",
            {"generation_id": generation.generation_id, "sequence": generation.sequence},
        )

        return {
            "runtime-snapshot.json": snapshot,
            "replay-recording.json": _scrub(recording.to_dict(), rename),
            "planning.json": _scrub(planning.to_dict(), rename),
            "reconciliation.json": _scrub(reconciled.to_dict(), rename),
            "config-apply.json": _scrub(applied.to_dict(), rename),
            "generation-pressure.json": pressure,
        }
    finally:
        await harness.stop()


def render_documents() -> dict[str, bytes]:
    assert chassis.__version__ == PACKAGE_VERSION, (
        f"generate with chassis-harness {PACKAGE_VERSION}, found {chassis.__version__}"
    )
    parsed = parse_config(CONFIGURATION)
    assert parsed.version == 1

    runtime = asyncio.run(_runtime_documents())
    documents = {name: _json_bytes(document) for name, document in runtime.items()}
    documents["configuration.yaml"] = CONFIGURATION.encode()

    manifest = {
        "candidate": True,
        "package_version": PACKAGE_VERSION,
        "fixtures": {
            "configuration": {"file": "configuration.yaml", "schema_version": 1},
            "config_apply": {"file": "config-apply.json", "format_version": 1},
            "diagnostics": {"file": "generation-pressure.json", "format_version": 1},
            "planning": {"file": "planning.json", "format_version": 1},
            "reconciliation": {"file": "reconciliation.json", "format_version": 1},
            "replay": {"file": "replay-recording.json", "format_version": 1},
            "snapshot": {"file": "runtime-snapshot.json", "format_version": 1},
        },
    }
    documents["manifest.json"] = _json_bytes(manifest)
    checksums = [
        f"{hashlib.sha256(content).hexdigest()}  {name}"
        for name, content in sorted(documents.items())
    ]
    documents["SHA256SUMS"] = ("\n".join(checksums) + "\n").encode()
    return documents


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if checked-in fixtures differ; do not write files",
    )
    args = parser.parse_args()
    documents = render_documents()

    if args.check:
        stale = [
            name
            for name, expected in documents.items()
            if not (HERE / name).is_file() or (HERE / name).read_bytes() != expected
        ]
        if stale:
            raise SystemExit("stale 1.0.0b2 format fixtures: " + ", ".join(stale))
        print("7 format fixtures, manifest, and checksums are current")
        return

    for name, content in documents.items():
        (HERE / name).write_bytes(content)
    print(documents["SHA256SUMS"].decode(), end="")


if __name__ == "__main__":
    main()
