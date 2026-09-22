"""Documentation links must resolve, and guarantee rows must name real tests.

A broken link is the first defect a reader hits and the only check that never
reports it, so it is asserted here: every relative link in the README, the
changelog, and ``docs/`` must point at a file that exists. Anchors and external
URLs are out of scope (a heading rename is cheaper to catch by reading).

R023's design table maps every guarantee to its enforcement. A row that merely
names a test file proves nothing — any file with some ``def test_`` would pass —
so every row must name a concrete ``tests/foo.py::test_name`` node and that node
must come out of a real pytest collection. The range grows only when a new
guarantee lands (G25 to G27 are the 0.9.0 contracts), and this test pins it.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = [
    ROOT / "README.md",
    ROOT / "CHANGELOG.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    *sorted((ROOT / "docs").glob("*.md")),
]
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
FENCE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)


def prose(document: Path) -> str:
    """Document text with code blocks removed: only links are under test."""

    return FENCE.sub("", document.read_text(encoding="utf-8"))


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
def test_relative_links_resolve(document: Path) -> None:
    missing: list[str] = []
    for target in LINK.findall(prose(document)):
        target = target.split("#", 1)[0].strip()
        if not target or "://" in target or target.startswith(("mailto:", "#")):
            continue
        if not (document.parent / target).exists():
            missing.append(target)

    assert missing == [], f"{document.name} links to missing files: {missing}"


GUARANTEE_ROW = re.compile(r"^\| (G\d+) \| (.*?) \| (.*?) \|$", re.MULTILINE)
TEST_REF = re.compile(r"(tests/[A-Za-z0-9_./-]+\.py)(?:::(test_[A-Za-z0-9_]+))?")


def collected_test_nodes(paths: list[str]) -> set[str]:
    """Node ids pytest collects from ``paths``, from one real collection run."""

    if not paths:
        return set()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--continue-on-collection-errors",
            *paths,
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    return {
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().startswith("tests/") and "::" in line
    }


def guarantee_violations(rows: list[tuple[str, str, str]]) -> list[str]:
    """Why the design-table rows fail R023 — empty only for real collected nodes."""

    violations: list[str] = []
    named: set[str] = set()
    wanted: list[tuple[str, str]] = []
    for name, claim, enforcement in rows:
        refs: list[tuple[str, str]] = TEST_REF.findall(enforcement)
        if not refs:
            violations.append(f"{name} maps to no test node: {claim}")
            continue
        for path_text, node in refs:
            if not node:
                violations.append(f"{name} names {path_text} with no ::test_name node")
            if not (ROOT / path_text).exists():
                violations.append(f"{name} references missing {path_text}")
            if node and (ROOT / path_text).exists():
                named.add(path_text)
                wanted.append((f"{path_text}::{node}", name))

    collected = collected_test_nodes(sorted(named))
    for node_id, name in wanted:
        if not any(item == node_id or item.startswith(f"{node_id}[") for item in collected):
            violations.append(f"{name} names node {node_id} which pytest does not collect")
    return violations


def test_every_guarantee_maps_to_an_existing_test_node() -> None:
    """Each G-row names a real collected ``file::test_name`` node, not just a file."""

    design = (ROOT / "docs" / "design.md").read_text(encoding="utf-8")
    rows: list[tuple[str, str, str]] = GUARANTEE_ROW.findall(design)
    names = [name for name, _claim, _enforcement in rows]

    assert names == [f"G{i}" for i in range(1, 28)]

    violations = guarantee_violations(rows)
    assert not violations, (
        "R023 rows whose enforcement is not a collected test node:\n" + "\n".join(violations)
    )
