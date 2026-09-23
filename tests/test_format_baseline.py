"""The 1.0 beta format candidates match the implementation exactly."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from chassis.config import load_config
from chassis.persistence import (
    DIAGNOSTICS_FORMAT_VERSION,
    PLAN_FORMAT_VERSION,
    REPLAY_FORMAT_VERSION,
    SNAPSHOT_FORMAT_VERSION,
    RuntimeSnapshot,
)
from chassis.replay import BoundaryKind, ReplaySession

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "compat" / "v1.0.0b1"


def _json(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_the_generator_reproduces_every_checked_in_candidate_byte_for_byte() -> None:
    result = subprocess.run(
        [sys.executable, str(FIXTURES / "generate.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "7 format fixtures, manifest, and checksums are current" in result.stdout


def test_the_manifest_covers_every_documented_format_family() -> None:
    manifest = _json("manifest.json")

    assert manifest["package_version"] == "1.0.0b1"
    assert manifest["candidate"] is True
    assert set(manifest["fixtures"]) == {
        "configuration",
        "config_apply",
        "diagnostics",
        "planning",
        "reconciliation",
        "replay",
        "snapshot",
    }
    for metadata in manifest["fixtures"].values():
        assert (FIXTURES / metadata["file"]).is_file()


def test_every_versioned_candidate_declares_the_current_format() -> None:
    expected = {
        "runtime-snapshot.json": SNAPSHOT_FORMAT_VERSION,
        "replay-recording.json": REPLAY_FORMAT_VERSION,
        "planning.json": PLAN_FORMAT_VERSION,
        "reconciliation.json": DIAGNOSTICS_FORMAT_VERSION,
        "config-apply.json": DIAGNOSTICS_FORMAT_VERSION,
        "generation-pressure.json": DIAGNOSTICS_FORMAT_VERSION,
    }

    for name, version in expected.items():
        assert _json(name)["format_version"] == version == 1


def test_the_snapshot_candidate_round_trips_exactly() -> None:
    document = _json("runtime-snapshot.json")

    assert document["chassis_version"] == "1.0.0b1"
    rebuilt = RuntimeSnapshot.from_dict(document)
    assert rebuilt.plugin_entries is None
    assert rebuilt.to_dict() == document


def test_the_replay_candidate_round_trips_exactly() -> None:
    document = _json("replay-recording.json")
    rebuilt = ReplaySession.from_dict(document)

    assert rebuilt.to_dict() == document
    assert {record.kind for record in rebuilt.records} == {
        BoundaryKind.LIFECYCLE,
        BoundaryKind.SNAPSHOT,
        BoundaryKind.TOOL,
    }


def test_the_configuration_candidate_preserves_values_but_exports_only_keys() -> None:
    config = load_config(FIXTURES / "configuration.yaml")

    assert config.version == 1
    assert [entry.id for entry in config.plugins] == ["orders-db", "memory"]
    assert config.plugins[0].config["endpoint"] == "https://example.invalid/orders"
    assert config.to_dict()["plugins"][0]["config_keys"] == ["endpoint"]
    assert config.provider_preferences == {"database": "orders-db"}


def test_operational_candidates_exercise_representative_nested_shapes() -> None:
    planning = _json("planning.json")
    reconciliation = _json("reconciliation.json")
    applied = _json("config-apply.json")
    pressure = _json("generation-pressure.json")

    assert planning["chassis_version"] == "1.0.0b1"
    assert planning["actions"][-1]["action"] == "publish"
    assert reconciliation["plan"]["edges"] == [["orders-db", "memory"]]
    assert reconciliation["impact"]["counts"] == {"added": 2}
    assert [change["action"] for change in applied["applied"]] == ["add", "add"]
    assert pressure["live_generations"] == 1
    assert {resource["entry_id"] for resource in pressure["resources"]} == {
        "memory",
        "orders-db",
    }


def test_candidate_fixtures_contain_no_secret_or_machine_specific_material() -> None:
    for path in sorted(FIXTURES.iterdir()):
        if not path.is_file() or path.name in {"README.md", "generate.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert "sk-" not in text, path.name
        assert "/Users/" not in text, path.name
        assert "/home/" not in text, path.name
