"""Persisted formats carry explicit versions and reject what they cannot read.

Round-trip exactness for the current format, explicit migration dispatch from
the pre-versioning 0.8.1 shape, and typed rejection of future, malformed, and
corrupted payloads — never a silent reinterpretation (guarantee G25).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from chassis import FormatError
from chassis.persistence import (
    DIAGNOSTICS_FORMAT_VERSION,
    REPLAY_FORMAT_VERSION,
    SNAPSHOT_FORMAT_VERSION,
    RuntimeSnapshot,
    declared_format_version,
    migrate_payload,
)
from chassis.replay import BoundaryKind, ReplayMode, ReplaySession

SNAPSHOT_FIELDS: dict[str, Any] = {
    "chassis_version": "0.9.0",
    "generation_id": "gen_0003",
    "sequence": 3,
    "agent_runtime": "langgraph",
    "agent": "support-agent",
    "agent_revision": "17",
    "plugins": {"orders-db": "1.0.0"},
    "capabilities": {"database": ["1"]},
    "config_hash": "a" * 64,
    "plugin_graph_hash": "b" * 64,
    "tool_schema_hash": "c" * 64,
    "graph_definition_hash": None,
    "prompt_hash": None,
    "metadata": {"dataset": "unit"},
    "scopes": {"root": "/", "scopes": []},
    "semantic_scopes": {"root": "/", "scopes": []},
    "runtime_instance_ids": ["plugin_x", "plugin_y"],
}


def snapshot_record(**overrides: Any) -> RuntimeSnapshot:
    fields = dict(SNAPSHOT_FIELDS)
    fields.update(overrides)
    fields.setdefault("created_at", 0.0)
    return RuntimeSnapshot(**fields)


def recording_document() -> ReplaySession:
    session = ReplaySession(mode=ReplayMode.RECORD, metadata={"dataset": "unit"})
    session.record(
        BoundaryKind.TOOL,
        key="tool-key",
        request={"tool": "lookup", "args": {"order": "A-1"}},
        response={"name": "lookup", "content": {"status": "shipped"}, "redacted": False},
    )
    return session


def test_a_snapshot_record_round_trips_exactly() -> None:
    snapshot = snapshot_record()

    payload = snapshot.to_dict()
    rebuilt = RuntimeSnapshot.from_dict(payload)

    assert rebuilt.to_dict() == payload
    assert rebuilt.digest() == snapshot.digest()
    assert rebuilt.semantic_digest() == snapshot.semantic_digest()
    assert rebuilt.generation_id == "gen_0003"
    assert rebuilt.agent_identity == "support-agent@17"
    assert rebuilt.capabilities == {"database": ("1",)}
    assert rebuilt.runtime_instance_ids == ("plugin_x", "plugin_y")


def test_a_recording_round_trips_exactly() -> None:
    session = recording_document()

    payload = session.to_dict()
    rebuilt = ReplaySession.from_dict(payload)

    assert rebuilt.to_dict() == payload
    assert rebuilt.mode is ReplayMode.RECORD
    assert [record.to_dict() for record in rebuilt.records] == [
        record.to_dict() for record in session.records
    ]


def test_every_serialized_document_declares_its_format_version() -> None:
    assert snapshot_record().to_dict()["format_version"] == SNAPSHOT_FORMAT_VERSION
    assert recording_document().to_dict()["format_version"] == REPLAY_FORMAT_VERSION

    async def reconcile_export() -> dict[str, Any]:
        from chassis.plugins import PluginContext, plugin
        from chassis.testing import TestHarness

        @plugin(name="noop", version="1.0.0")
        async def noop(ctx: PluginContext) -> None:
            return None

        export: dict[str, Any] = {}
        async with TestHarness() as harness:
            harness.install(noop, entry_id="noop")
            await harness.reconcile()
            result = await harness.reconcile()
            assert result is not None
            export = result.to_dict()
        return export

    payload = asyncio.run(reconcile_export())
    assert payload["format_version"] == DIAGNOSTICS_FORMAT_VERSION


def test_a_future_version_is_rejected() -> None:
    for found in (2, 99):
        payload = snapshot_record().to_dict()
        payload["format_version"] = found

        with pytest.raises(FormatError) as excinfo:
            RuntimeSnapshot.from_dict(payload)

        assert excinfo.value.context["reason"] == "future_version"
        assert excinfo.value.context["format"] == "snapshot"
        assert excinfo.value.context["found"] == found
        assert excinfo.value.context["supported"] == SNAPSHOT_FORMAT_VERSION


@pytest.mark.parametrize("found", ["1", -1, 1.5, None, True], ids=repr)
def test_a_malformed_version_is_rejected(found: Any) -> None:
    payload = recording_document().to_dict()
    payload["format_version"] = found

    with pytest.raises(FormatError) as excinfo:
        ReplaySession.from_dict(payload)

    assert excinfo.value.context["reason"] == "malformed_version"
    assert excinfo.value.context["format"] == "replay"


def test_a_corrupted_snapshot_payload_is_rejected() -> None:
    payload = snapshot_record().to_dict()
    del payload["generation_id"]

    with pytest.raises(FormatError) as missing:
        RuntimeSnapshot.from_dict(payload)
    assert missing.value.context["reason"] == "corrupted"
    assert "generation_id" in str(missing.value)

    wrong_type = snapshot_record().to_dict()
    wrong_type["sequence"] = "three"
    with pytest.raises(FormatError) as excinfo:
        RuntimeSnapshot.from_dict(wrong_type)
    assert excinfo.value.context["reason"] == "corrupted"


def test_a_corrupted_recording_is_rejected() -> None:
    payload = recording_document().to_dict()
    payload["records"][0] = {"kind": "not-a-boundary", "sequence": 0, "key": "k"}

    with pytest.raises(FormatError) as excinfo:
        ReplaySession.from_dict(payload)
    assert excinfo.value.context["reason"] == "corrupted"

    with pytest.raises(FormatError) as not_a_document:
        ReplaySession.from_dict({"mode": "record", "records": "everything"})
    assert not_a_document.value.context["reason"] == "corrupted"


def test_a_corrupted_file_is_rejected(tmp_path: Any) -> None:
    path = tmp_path / "recording.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(FormatError) as excinfo:
        ReplaySession.load(path)

    assert excinfo.value.context["reason"] == "corrupted"


def test_a_legacy_payload_is_migrated_through_a_named_step() -> None:
    seen: list[int] = []

    def step(version: int) -> Any:
        def migrate(document: dict[str, Any]) -> dict[str, Any]:
            seen.append(version)
            return dict(document)

        return migrate

    migrated = migrate_payload(
        {"payload": 1},
        format_name="synthetic",
        supported=3,
        migrations={0: step(0), 1: step(1), 2: step(2)},
    )

    assert seen == [0, 1, 2]
    assert migrated == {"payload": 1, "format_version": 3}


def test_a_missing_migration_step_is_an_explicit_refusal() -> None:
    with pytest.raises(FormatError) as excinfo:
        migrate_payload(
            {"format_version": 1},
            format_name="synthetic",
            supported=3,
            migrations={2: lambda document: document},
        )

    assert excinfo.value.context["reason"] == "unmigratable"
    assert excinfo.value.context["found"] == 1


def test_the_current_version_needs_no_migration() -> None:
    assert (
        declared_format_version(
            {"format_version": SNAPSHOT_FORMAT_VERSION},
            format_name="snapshot",
            supported=SNAPSHOT_FORMAT_VERSION,
        )
        == SNAPSHOT_FORMAT_VERSION
    )
    assert (
        declared_format_version({}, format_name="snapshot", supported=SNAPSHOT_FORMAT_VERSION) == 0
    )


def test_documents_are_json_compatible() -> None:
    for payload in (snapshot_record().to_dict(), recording_document().to_dict()):
        assert json.loads(json.dumps(payload, sort_keys=True)) == json.loads(json.dumps(payload))
