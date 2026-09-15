"""Scope-owned background work.

Plugins must not create unowned immortal tasks. Every task a plugin starts goes
through :class:`ScopedTasks`, which delegates to the plugin scope: the scope
cancels and awaits the task when the plugin unloads, and reports tasks that die
with an exception instead of letting the failure disappear.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

from chassis.core.scope import Scope

__all__ = ["ScopedTasks"]

T = TypeVar("T")


class ScopedTasks:
    """Task API bound to one owning scope."""

    __slots__ = ("_scope",)

    def __init__(self, scope: Scope) -> None:
        self._scope = scope

    @property
    def scope(self) -> Scope:
        return self._scope

    @property
    def tasks(self) -> tuple[asyncio.Task[Any], ...]:
        """Tasks currently owned by the bound scope."""

        return self._scope.tasks

    @property
    def is_open(self) -> bool:
        """Whether new tasks may still be started."""

        return self._scope.is_open

    def create_task(
        self, coro: Coroutine[Any, Any, T], *, name: str | None = None
    ) -> asyncio.Task[T]:
        """Start a background task owned by the bound scope."""

        return self._scope.create_task(coro, name=name)
