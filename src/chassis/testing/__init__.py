"""Testing utilities for plugin and agent authors.

The core doubles (:class:`FakePolicy`, :class:`FakeSecrets`, :class:`FakeTelemetry`
and :class:`TestHarness`) import nothing outside the Chassis core, so a plugin's
lifecycle tests need no optional extras. The ``langchain-core`` doubles
(:class:`FakeChatModel`, :func:`fake_tool`) are imported lazily, so importing this
package stays cheap and works without the ``langgraph`` extra.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from chassis.testing.fakes import FakePolicy, FakeSecrets, FakeTelemetry
from chassis.testing.harness import TestHarness

if TYPE_CHECKING:
    from chassis.testing.langchain import FakeChatModel, fake_tool

__all__ = [
    "FakeChatModel",
    "FakePolicy",
    "FakeSecrets",
    "FakeTelemetry",
    "TestHarness",
    "fake_tool",
]

_LAZY = {
    "FakeChatModel": "chassis.testing.langchain",
    "fake_tool": "chassis.testing.langchain",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module_name), name)
