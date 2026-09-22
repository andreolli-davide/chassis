"""Registration validates the full tool contract at construction time (R018).

``description`` is part of the mandatory contract: a tool without one must be
rejected when it is registered, not later with an ``AttributeError`` while a
snapshot or diagnostic view is being built.
"""

from __future__ import annotations

import pytest
from langchain_core.tools import tool

from chassis.core.errors import ConfigurationError
from chassis.core.scope import Scope
from chassis.tools import ToolRegistry


@tool
def ping() -> str:
    """Respond to a liveness probe."""

    return "pong"


class MissingDescription:
    name = "missing-description"

    async def ainvoke(self, input: object, config: object = None, **kwargs: object) -> str:
        return "ok"


class EmptyDescription:
    name = "empty-description"
    description = ""

    async def ainvoke(self, input: object, config: object = None, **kwargs: object) -> str:
        return "ok"


class NonStringDescription:
    name = "non-string-description"
    description = 123

    async def ainvoke(self, input: object, config: object = None, **kwargs: object) -> str:
        return "ok"


def test_a_tool_without_a_valid_description_is_rejected_at_registration() -> None:
    registry = ToolRegistry()
    scope = Scope("owner")

    for bad in (MissingDescription(), EmptyDescription(), NonStringDescription()):
        with pytest.raises(ConfigurationError):
            registry.register(scope=scope, tool=bad)  # type: ignore[arg-type]

    entry = registry.register(scope=scope, tool=ping)
    assert entry.name == "ping"
    assert entry.description == "Respond to a liveness probe."
