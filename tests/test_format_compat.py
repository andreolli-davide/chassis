"""0.9.0 reads what the released 0.8.1 implementation wrote.

The fixtures in ``tests/compat/v0.8.1/`` were produced by the 0.8.1 code from
the `v0.8.1` tag (see the README there for provenance and sanitization). These
tests load every one with the 0.9.0 readers and assert field by field that
migration preserves semantic attribution: generation identity, agent identity
and revision, capability versions, replay boundary kind and key, redaction
status, and tool/model result semantics.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from chassis.config import load_config
from chassis.persistence import RuntimeSnapshot
from chassis.replay import BoundaryKind, ReplayMode, ReplaySession

FIXTURES = Path(__file__).resolve().parent / "compat" / "v0.8.1"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_the_fixtures_are_pre_versioning_0_8_1_documents() -> None:
    """Provenance guard: these are the shapes 0.8.1 actually produced."""

    for name in ("runtime-snapshot.json", "replay-recording.json"):
        document = _fixture(name)
        assert "format_version" not in document, name

    assert _fixture("runtime-snapshot.json")["chassis_version"] == "0.8.1"
    assert any(
        record["response"].get("chassis_version") == "0.8.1"
        for record in _fixture("replay-recording.json")["records"]
        if record["kind"] == "snapshot"
    )


def test_the_0_8_1_snapshot_migrates_without_losing_attribution() -> None:
    document = _fixture("runtime-snapshot.json")

    snapshot = RuntimeSnapshot.from_dict(document)
    record = snapshot.to_dict()

    # Generation identity and sequence.
    assert snapshot.generation_id == "gen_0002"
    assert snapshot.sequence == 2
    assert snapshot.chassis_version == "0.8.1"

    # Agent identity and revision.
    assert snapshot.agent == "support-agent"
    assert snapshot.agent_revision is None
    assert snapshot.agent_identity == "support-agent"

    # Plugin identity and capability versions.
    assert dict(snapshot.plugins) == {"memory": "2.1.0", "orders-db": "1.0.0"}
    assert {name: list(versions) for name, versions in snapshot.capabilities.items()} == {
        "database": ["1"],
        "memory": ["1"],
    }

    # The hash family and metadata keys survive exactly.
    assert snapshot.config_hash == document["config_hash"]
    assert snapshot.plugin_graph_hash == document["plugin_graph_hash"]
    assert snapshot.tool_schema_hash == document["tool_schema_hash"]
    assert dict(snapshot.metadata) == {"dataset": "compat-fixtures"}
    assert snapshot.runtime_instance_ids == ("plugin_fixture_0", "plugin_fixture_1")

    # The physical scope tree is preserved one-for-one.
    assert record["scopes"] == document["scopes"]
    recorded_scope = document["scopes"]["scopes"][0]
    assert recorded_scope["providers"] == {
        "database": ["plugin_fixture_0"],
        "memory": ["plugin_fixture_1"],
    }
    selection = recorded_scope["selections"][0]
    assert selection == {
        "consumer": "memory",
        "provider": "orders-db",
        "provider_scope": "/",
        "requirement": "database <2,>=1",
        "status": "resolved",
    }


def test_the_migrated_snapshot_reconstructs_the_semantic_scope_view() -> None:
    document = _fixture("runtime-snapshot.json")

    snapshot = RuntimeSnapshot.from_dict(document)

    semantic = snapshot.to_dict()["semantic_scopes"]
    assert semantic["root"] == "/"
    scope = semantic["scopes"][0]
    assert scope["path"] == "/"
    assert scope["entries"] == ["memory", "orders-db"]
    assert scope["selections"] == document["scopes"]["scopes"][0]["selections"]
    # The documented limitation: 0.8.1 recorded provider *instance* ids and
    # persisted no instance-to-entry mapping, so provider identity is reported
    # empty rather than guessed.
    assert scope["providers"] == {}
    assert snapshot.semantic_digest() == snapshot.semantic_digest()


def test_the_migrated_snapshot_declares_the_current_format() -> None:
    payload = RuntimeSnapshot.from_dict(_fixture("runtime-snapshot.json")).to_dict()

    assert payload["format_version"] == 1
    assert payload["generation_id"] == "gen_0002"
    assert payload["runtime_instance_ids"] == ["plugin_fixture_0", "plugin_fixture_1"]


def test_the_0_8_1_recording_migrates_without_losing_attribution() -> None:
    session = ReplaySession.from_dict(_fixture("replay-recording.json"))

    assert session.mode is ReplayMode.RECORD
    assert session.metadata == {
        "dataset": "compat-fixtures",
        "produced_by": "chassis-harness 0.8.1",
    }

    records = session.records
    assert len(records) == 13

    # Boundary kind and key are preserved for every interaction.
    by_kind = {kind: [record for record in records if record.kind is kind] for kind in BoundaryKind}
    assert [record.sequence for record in records] == list(range(13))
    assert len({record.key for record in by_kind[BoundaryKind.TOOL]}) == 1
    assert len({record.key for record in by_kind[BoundaryKind.MODEL]}) == 1

    # Tool semantics and redaction status.
    tool = by_kind[BoundaryKind.TOOL][0]
    assert tool.request == {"tool": "lookup_order", "args": {"order": "A-1"}}
    assert tool.response["content"] == {"order": "A-1", "status": "shipped"}
    assert tool.response["artifact"] is None
    assert tool.response["error"] is None
    assert tool.response["status"] == "ok"
    assert tool.response["redacted"] is False
    assert tool.response["name"] == "lookup_order"

    # Model semantics.
    model = by_kind[BoundaryKind.MODEL][0]
    assert model.request["model"] == "fake-model"
    assert model.request["messages"][0]["content"] == "where is order A-1?"
    assert model.request["options"] == {"stop": None}
    generation = model.response["generations"][0]
    assert generation["message"]["content"] == "Order A-1 shipped."
    assert model.response["llm_output"] is None

    # Lifecycle and snapshot records remain attribution only.
    events = [record.request["event"] for record in by_kind[BoundaryKind.LIFECYCLE]]
    assert "generation.publish" in events
    assert "plugin.mount" in events
    assert "generation.draining" in events
    assert "generation.retired" in events
    assert "harness.shutdown" in events
    assert "plugin.unmount" in events
    assert {record.response["generation_id"] for record in by_kind[BoundaryKind.SNAPSHOT]} <= {
        "gen_0001",
        "gen_0002",
    }

    # The migrated document round-trips in the current format.
    migrated = session.to_dict()
    assert migrated["format_version"] == 1
    assert ReplaySession.from_dict(migrated).to_dict() == migrated


def test_the_0_8_1_configuration_document_still_parses() -> None:
    config = load_config(FIXTURES / "configuration.yaml")

    assert config.version == 1
    assert [entry.id for entry in config.plugins] == ["model", "orders"]
    assert config.plugins[0].plugin == "support-model"
    assert set(config.plugins[0].config) == {"model", "api_key"}
    assert config.provider_preferences == {"database": "orders-db"}


def test_fixtures_contain_no_secret_or_machine_specific_material() -> None:
    for path in sorted(FIXTURES.glob("*")):
        if path.name in {"README.md", "generate.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert "sk-" not in text, path.name
        assert "/Users/" not in text, path.name
        assert "/home/" not in text, path.name
        assert "plugin_fixture" in text or path.suffix == ".yaml", path.name
