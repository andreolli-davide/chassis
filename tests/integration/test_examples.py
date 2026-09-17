"""Run the shipped examples, so documentation cannot drift from behaviour.

Each example asserts the behaviour it prints, so executing it is the verification.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def load_example(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"chassis_example_{name}", EXAMPLES / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "name",
    [
        "quickstart",
        "basic_agent",
        "reactive_cascade",
        "safe_provider_replacement",
        "scoped_composition",
        "agent_composition",
    ],
)
async def test_example_runs_and_verifies_itself(name: str) -> None:
    module = load_example(name)

    await module.main()


def test_examples_directory_contains_the_shipped_examples() -> None:
    names = {path.stem for path in EXAMPLES.glob("*.py")}

    assert {
        "quickstart",
        "basic_agent",
        "reactive_cascade",
        "safe_provider_replacement",
        "scoped_composition",
        "agent_composition",
    } <= names
