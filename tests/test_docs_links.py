"""Documentation links and anchors must resolve, and guarantees name real tests.

A broken link is the first defect a reader hits and the only check that never
reports it, so it is asserted here: every relative link in the README, the
changelog, and ``docs/`` must point at a file that exists, and every local Markdown
anchor must name a heading in its target. External URLs remain out of scope.

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
import unicodedata
from html import unescape
from pathlib import Path
from urllib.parse import unquote

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
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
EXPLICIT_ID = re.compile(r"\s*\{#([A-Za-z][A-Za-z0-9_:.-]*)[^}]*\}\s*$")
MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
HTML_TAG = re.compile(r"<[^>]+>")


def prose(document: Path) -> str:
    """Document text with code blocks removed: only links are under test."""

    return FENCE.sub("", document.read_text(encoding="utf-8"))


def _heading_slug(value: str) -> str:
    """Return the Python-Markdown/MkDocs-style implicit id for one heading."""

    value = MARKDOWN_LINK.sub(r"\1", value)
    value = HTML_TAG.sub("", value)
    value = unescape(value).replace("`", "")
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^\w\s-]", "", value.lower())
    return re.sub(r"[-\s]+", "-", value).strip("-")


def heading_ids(document: Path) -> set[str]:
    """Heading ids generated for a document, including duplicate suffixes."""

    identifiers: set[str] = set()
    counts: dict[str, int] = {}
    for heading in HEADING.findall(prose(document)):
        explicit = EXPLICIT_ID.search(heading)
        base = explicit.group(1) if explicit is not None else _heading_slug(heading)
        count = counts.get(base, 0)
        identifier = base if count == 0 else f"{base}_{count}"
        counts[base] = count + 1
        identifiers.add(identifier)
    return identifiers


def test_heading_ids_match_mkdocs_implicit_explicit_and_duplicate_rules(tmp_path: Path) -> None:
    document = tmp_path / "headings.md"
    document.write_text(
        "# 0.9.0 — production confidence\n"
        "## Repeated heading\n"
        "## Repeated heading\n"
        "## Display name {#stable-id}\n",
        encoding="utf-8",
    )

    assert heading_ids(document) == {
        "090-production-confidence",
        "repeated-heading",
        "repeated-heading_1",
        "stable-id",
    }


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


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
def test_local_markdown_anchors_resolve(document: Path) -> None:
    """A local ``file.md#heading`` link must name a real generated heading id."""

    missing: list[str] = []
    for target in LINK.findall(prose(document)):
        path_text, separator, fragment = target.partition("#")
        if not separator or not fragment or "://" in target or target.startswith("mailto:"):
            continue
        target_document = document if not path_text else document.parent / path_text
        if target_document.suffix.lower() != ".md" or not target_document.exists():
            continue
        identifier = unquote(fragment)
        if identifier not in heading_ids(target_document):
            missing.append(target)

    assert missing == [], f"{document.name} links to missing Markdown anchors: {missing}"


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
