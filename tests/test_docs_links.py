"""Documentation links must resolve.

A broken link is the first defect a reader hits and the only check that never
reports it, so it is asserted here: every relative link in the README, the
changelog, and ``docs/`` must point at a file that exists. Anchors and external
URLs are out of scope (a heading rename is cheaper to catch by reading).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = [
    ROOT / "README.md",
    ROOT / "CHANGELOG.md",
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
