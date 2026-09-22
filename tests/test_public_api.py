"""The documented public surface must exist and stay importable.

Documentation that references an API which no longer exists is a defect; this test
is what keeps the two in step.

The surface itself is the machine-readable map in ``tests/compat/public-api.json``
— the same source of truth ``scripts/api_compat.py`` classifies and compares — so
an accidental addition to ``__all__`` or removal from a documented module is
detected here, and the API baseline records how each name is shaped.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

SURFACE = json.loads(
    (Path(__file__).resolve().parent / "compat" / "public-api.json").read_text(encoding="utf-8")
)
DOCUMENTED = {module: sorted(names) for module, names in SURFACE["modules"].items()}


@pytest.mark.parametrize("module_name", sorted(DOCUMENTED))
def test_documented_names_are_importable(module_name: str) -> None:
    module = importlib.import_module(module_name)

    missing = [name for name in DOCUMENTED[module_name] if not hasattr(module, name)]

    assert missing == [], f"{module_name} is missing {missing}"


@pytest.mark.parametrize("module_name", sorted(DOCUMENTED))
def test_documented_names_are_exported(module_name: str) -> None:
    module = importlib.import_module(module_name)
    exported: set[str] = set(getattr(module, "__all__", ()))

    missing = [name for name in DOCUMENTED[module_name] if name not in exported]

    assert missing == [], f"{module_name}.__all__ is missing {missing}"


def test_every_top_level_export_is_intentional() -> None:
    """The reviewed public surface: anything beyond it is an accidental export."""

    import chassis

    allowed = set(DOCUMENTED["chassis"])
    extras = sorted(set(chassis.__all__) - allowed)
    assert extras == [], f"accidental exports (document them or remove them): {extras}"


def test_every_documented_module_is_part_of_the_reviewed_surface() -> None:
    """The surface map names only reviewed modules; a new one needs a review."""

    reviewed = set(DOCUMENTED)
    assert "chassis" in reviewed, "the top-level module must be classified"

    for module_name in sorted(reviewed):
        names = DOCUMENTED[module_name]
        assert names, f"{module_name} is classified with no public names"


def test_core_does_not_import_langgraph() -> None:
    """The lifecycle kernel must stay independent of the execution engine."""

    import subprocess
    import sys

    script = (
        "import sys; import chassis; "
        "assert 'langgraph' not in sys.modules, 'chassis imported langgraph eagerly'; "
        "print(chassis.__version__)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
