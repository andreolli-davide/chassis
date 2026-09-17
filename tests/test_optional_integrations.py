"""The core must work without the optional agent integrations.

These tests run in a subprocess with ``langgraph``, ``langchain_core``, and
``langsmith`` made unimportable, which is the only honest way to prove that
``import chassis`` and the lifecycle kernel do not depend on them. A missing extra
must produce an actionable error, not a bare ``ImportError`` or, worse, an
accidental import of an integration module from the core.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")

_BLOCKER = """
import sys, importlib.abc

_BLOCKED = {"langgraph", "langchain_core", "langsmith"}


class _BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in _BLOCKED:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


sys.meta_path.insert(0, _BlockOptional())
"""


def run_without_extras(script: str) -> subprocess.CompletedProcess[str]:
    """Run ``script`` in an interpreter where the optional integrations are absent."""

    env = {**os.environ, "PYTHONPATH": SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}
    return subprocess.run(
        [sys.executable, "-c", _BLOCKER + script],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


CORE_LIFECYCLE = """
import asyncio
import chassis
from chassis import Harness, PluginContext, plugin


@plugin(name="resource", version="1.0.0", provides={"database": "1.0.0"})
async def resource(ctx: PluginContext) -> None:
    ctx.capabilities.provide(chassis.DATABASE, {"dsn": "postgres://local"})


async def main() -> None:
    harness = Harness()
    harness.install(resource, entry_id="db")
    await harness.start()
    generation = harness.current_generation
    assert generation is not None
    async with harness.acquire() as acquired:
        assert acquired is generation
        assert generation.lease_count == 1
        assert harness.diagnostics.status()["state"] == "running"
    assert generation.lease_count == 0
    assert harness.diagnostics.generations()
    await harness.stop()
    print("core lifecycle ok")


asyncio.run(main())
"""


def test_core_imports_and_runs_without_optional_integrations() -> None:
    result = run_without_extras(CORE_LIFECYCLE)

    assert result.returncode == 0, result.stderr
    assert "core lifecycle ok" in result.stdout


def test_core_import_does_not_touch_optional_modules() -> None:
    result = run_without_extras(
        "import sys, chassis, chassis.replay, chassis.telemetry, chassis.testing\n"
        "import chassis.tools, chassis.agents, chassis.diagnostics\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'langgraph', 'langchain_core', 'langsmith'})\n"
        "assert loaded == [], loaded\n"
        "print('clean')\n"
    )

    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_langgraph_integration_reports_the_missing_extra() -> None:
    result = run_without_extras(
        "import chassis\n"
        "try:\n"
        "    import chassis.langgraph  # noqa: F401\n"
        "except ImportError as error:\n"
        "    print(type(error).__name__)\n"
        "    print(str(error))\n"
        "else:\n"
        "    raise SystemExit('chassis.langgraph imported without the extra')\n"
    )

    assert result.returncode == 0, result.stderr
    assert "MissingExtraError" in result.stdout
    assert "chassis-harness[langgraph]" in result.stdout


def test_langsmith_integration_is_optional_and_reports_the_missing_extra() -> None:
    result = run_without_extras(
        "from chassis.telemetry import LangSmithTelemetry\n"
        "disabled = LangSmithTelemetry(enabled=False)\n"
        "assert disabled.enabled is False\n"
        "try:\n"
        "    LangSmithTelemetry(enabled=True)\n"
        "except ImportError as error:\n"
        "    print(type(error).__name__)\n"
        "    print(str(error))\n"
        "else:\n"
        "    raise SystemExit('LangSmithTelemetry(enabled=True) did not require langsmith')\n"
    )

    assert result.returncode == 0, result.stderr
    assert "MissingExtraError" in result.stdout
    assert "chassis-harness[langsmith]" in result.stdout


def test_replay_model_reports_the_missing_extra() -> None:
    result = run_without_extras(
        "import chassis.replay\n"
        "try:\n"
        "    chassis.replay.ReplayChatModel\n"
        "except ImportError as error:\n"
        "    print(type(error).__name__)\n"
        "else:\n"
        "    raise SystemExit('ReplayChatModel resolved without langchain-core')\n"
    )

    assert result.returncode == 0, result.stderr
    assert "MissingExtraError" in result.stdout


def test_core_test_doubles_work_without_langchain() -> None:
    result = run_without_extras(
        "import chassis.testing as testing\n"
        "from chassis.testing import FakePolicy, FakeSecrets, TestHarness\n"
        "print('core doubles ok')\n"
        "try:\n"
        "    testing.fake_tool('echo')\n"
        "except ImportError as error:\n"
        "    print(type(error).__name__)\n"
        "else:\n"
        "    raise SystemExit('fake_tool resolved without langchain-core')\n"
    )

    assert result.returncode == 0, result.stderr
    assert "core doubles ok" in result.stdout
    assert "MissingExtraError" in result.stdout
