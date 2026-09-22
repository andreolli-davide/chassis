"""Scoped ownership of reversible effects.

Every harness-managed effect has exactly one owning :class:`Scope` (invariant I1).
A scope owns:

- cleanup callbacks (inverse operations registered by registries);
- entered (async) context managers;
- child scopes;
- background tasks;
- a diagnostic record for each effect it currently owns.

Teardown is deterministic: the scope stops accepting new work, cancels and awaits
owned tasks, then unwinds effects in reverse registration order. Individual
cleanup failures never abort the unwind; they are aggregated into
:class:`~chassis.core.errors.EffectCleanupError`.

``AsyncExitStack`` provides the LIFO bookkeeping; Chassis wraps every registered
entry so that a failing disposer is recorded instead of masking the remaining
disposers.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Callable, Coroutine, Mapping
from contextlib import AbstractAsyncContextManager, AbstractContextManager, AsyncExitStack
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Any, TypeVar

from chassis.core.errors import CleanupFailure, EffectCleanupError, ScopeClosedError

__all__ = ["EffectRecord", "Scope", "ScopeState"]

T = TypeVar("T")

_DEFAULT_TASK_SHUTDOWN_TIMEOUT = 5.0


class ScopeState(StrEnum):
    """Lifecycle state of a scope."""

    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class EffectRecord:
    """Diagnostic record for one effect currently owned by a scope.

    Effects are created by :meth:`Scope.cleanup`, :meth:`Scope.enter_context`,
    :meth:`Scope.enter_async_context`, :meth:`Scope.child` and by any registry
    that registers its inverse operation through the owning scope.
    """

    effect_id: str
    kind: str
    description: str
    scope_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "effect_id": self.effect_id,
            "kind": self.kind,
            "description": self.description,
            "scope_id": self.scope_id,
        }


class _Cleanup:
    """Adapts an arbitrary (possibly async) callable into an exit-stack entry.

    Failures are recorded on the owning scope instead of propagating, so that a
    single failing disposer cannot abort the remaining unwind.
    """

    __slots__ = ("_args", "_func", "_kwargs", "_record", "_scope")

    def __init__(
        self,
        scope: Scope,
        record: EffectRecord,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> None:
        self._scope = scope
        self._record = record
        self._func = func
        self._args = args
        self._kwargs = dict(kwargs)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        self._scope.release_effect(self._record)
        try:
            result = self._func(*self._args, **self._kwargs)
            if inspect.isawaitable(result):
                await result
        except Exception as error:
            self._scope.record_failure(self._record.description, error)
        return False


class _ContextCleanup:
    """Wraps a user-supplied context manager entered by a scope."""

    __slots__ = ("_cm", "_exit", "_record", "_scope")

    def __init__(
        self,
        scope: Scope,
        record: EffectRecord,
        exit_method: Callable[..., Any],
        cm: Any,
    ) -> None:
        self._scope = scope
        self._record = record
        self._exit = exit_method
        self._cm = cm

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        self._scope.release_effect(self._record)
        try:
            # A scope close is a *normal* exit: user context managers observe no
            # in-flight exception because Chassis cleanup never propagates one.
            result = self._exit(self._cm, None, None, None)
            if inspect.isawaitable(result):
                await result
        except Exception as error:
            self._scope.record_failure(self._record.description, error)
        return False


class Scope:
    """Owns reversible effects for one lifecycle unit (typically one plugin).

    Args:
        name: Human-readable scope name, used in diagnostics and errors.
        description: Optional longer description.
        parent: Owning parent scope, when this scope is a child.
        task_shutdown_timeout: Seconds to wait for owned tasks to stop during
            close. ``None`` waits indefinitely.
    """

    def __init__(
        self,
        name: str,
        *,
        description: str = "",
        parent: Scope | None = None,
        task_shutdown_timeout: float | None = _DEFAULT_TASK_SHUTDOWN_TIMEOUT,
    ) -> None:
        self._id = f"scope_{uuid.uuid4().hex[:12]}"
        self._name = name
        self._description = description
        self._parent = parent
        self._state = ScopeState.OPEN
        self._stack = AsyncExitStack()
        self._effects: dict[str, EffectRecord] = {}
        self._children: list[Scope] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._failures: list[CleanupFailure] = []
        self._close_task: asyncio.Task[None] | None = None
        self._task_shutdown_timeout = task_shutdown_timeout
        self._stragglers: tuple[asyncio.Task[Any], ...] = ()

    # ------------------------------------------------------------------ state

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parent(self) -> Scope | None:
        return self._parent

    @property
    def state(self) -> ScopeState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state is ScopeState.OPEN

    @property
    def is_closed(self) -> bool:
        return self._state is ScopeState.CLOSED

    @property
    def effects(self) -> tuple[EffectRecord, ...]:
        """Effects currently owned by this scope."""

        return tuple(self._effects.values())

    @property
    def children(self) -> tuple[Scope, ...]:
        return tuple(self._children)

    @property
    def tasks(self) -> tuple[asyncio.Task[Any], ...]:
        """Tasks currently owned by this scope that have not finished."""

        return tuple(self._tasks)

    @property
    def failures(self) -> tuple[CleanupFailure, ...]:
        """Failures recorded so far; aggregated into the close error."""

        return tuple(self._failures)

    @property
    def stragglers(self) -> tuple[asyncio.Task[Any], ...]:
        """Owned tasks still alive after close timed out waiting for them."""

        return self._stragglers

    @property
    def fully_disposed(self) -> bool:
        """Whether the scope closed with every owned effect and task finished.

        False for stragglers, for effects that were never released (a partial
        unwind), and for any recorded cleanup failure: a disposer that failed may
        still hold its resource, so the scope is never presented as fully
        disposed when one failed.
        """

        return (
            self._state is ScopeState.CLOSED
            and not self._stragglers
            and not self._effects
            and not self._failures
        )

    def assert_open(self, operation: str) -> None:
        """Raise :class:`ScopeClosedError` unless the scope accepts new work."""

        if self._state is not ScopeState.OPEN:
            raise ScopeClosedError(
                f"cannot {operation}: scope {self._name!r} is {self._state.value}",
                scope=self._name,
                scope_id=self._id,
                state=self._state.value,
            )

    def to_dict(self) -> dict[str, Any]:
        """Diagnostic snapshot of owned effects and tasks."""

        return {
            "id": self._id,
            "name": self._name,
            "description": self._description,
            "state": self._state.value,
            "parent": self._parent.id if self._parent is not None else None,
            "children": [child.id for child in self._children],
            "effects": [effect.to_dict() for effect in self._effects.values()],
            "tasks": [task.get_name() for task in self._tasks],
            "stragglers": [task.get_name() for task in self._stragglers],
            "fully_disposed": self.fully_disposed,
            "failures": [failure.description for failure in self._failures],
        }

    # --------------------------------------------------------------- effects

    def register_effect(self, kind: str, description: str) -> EffectRecord:
        """Record an owned effect without registering a disposer.

        Used by registries that maintain their own reverse index but still want
        the effect to be visible in diagnostics.
        """

        self.assert_open(f"register effect {description!r}")
        record = EffectRecord(
            effect_id=f"fx_{uuid.uuid4().hex[:12]}",
            kind=kind,
            description=description,
            scope_id=self._id,
        )
        self._effects[record.effect_id] = record
        return record

    def release_effect(self, record: EffectRecord) -> None:
        """Forget a previously registered effect (after manually reverting it)."""

        self._release_effect(record.effect_id)

    def cleanup(
        self,
        description: str,
        func: Callable[..., Any],
        *args: Any,
        kind: str = "effect",
        **kwargs: Any,
    ) -> EffectRecord:
        """Register the inverse of an operation performed through this scope.

        ``func`` may be sync or async; it is invoked once, in reverse
        registration order, when the scope closes.
        """

        record = self.register_effect(kind, description)
        handler = _Cleanup(self, record, func, args, kwargs)
        self._stack.push_async_exit(handler.__aexit__)
        return record

    def enter_context(self, cm: AbstractContextManager[T]) -> T:
        """Enter a synchronous context manager owned by this scope.

        A failed ``__enter__`` leaves no effect record behind: the scope never
        owns an effect it could not create.
        """

        record = self.register_effect("context", f"context {type(cm).__name__}")
        try:
            result = cm.__enter__()
        except BaseException:
            self.release_effect(record)
            raise
        handler = _ContextCleanup(self, record, type(cm).__exit__, cm)
        self._stack.push_async_exit(handler.__aexit__)
        return result

    async def enter_async_context(self, cm: AbstractAsyncContextManager[T]) -> T:
        """Enter an async context manager owned by this scope.

        A failed ``__aenter__`` leaves no effect record behind: the scope never
        owns an effect it could not create.
        """

        record = self.register_effect("context", f"async context {type(cm).__name__}")
        try:
            result = await cm.__aenter__()
        except BaseException:
            self.release_effect(record)
            raise
        if self._state is not ScopeState.OPEN:
            # The scope closed while entry was awaited, so the exit stack is
            # already unwound: exit the manager here rather than pushing a
            # disposer onto a closed stack, where it would never run.
            self.release_effect(record)
            await cm.__aexit__(None, None, None)
            self.assert_open("enter async context")
        handler = _ContextCleanup(self, record, type(cm).__aexit__, cm)
        self._stack.push_async_exit(handler.__aexit__)
        return result

    def child(self, name: str, *, description: str = "") -> Scope:
        """Create a child scope closed before this scope's earlier effects."""

        self.assert_open(f"create child scope {name!r}")
        child = Scope(
            name,
            description=description,
            parent=self,
            task_shutdown_timeout=self._task_shutdown_timeout,
        )
        self._children.append(child)
        self.cleanup(f"child scope {name!r}", child.aclose, kind="child")
        return child

    # ----------------------------------------------------------------- tasks

    def create_task(
        self,
        coro: Coroutine[Any, Any, T],
        *,
        name: str | None = None,
    ) -> asyncio.Task[T]:
        """Start a background task owned by this scope.

        Owned tasks are cancelled and awaited when the scope closes, so plugins
        cannot create immortal unowned work.
        """

        self.assert_open("create task")
        task = asyncio.ensure_future(coro)
        task.set_name(name or f"{self._name}#{len(self._tasks) + 1}")
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        # Always retrieve the exception: an unretrieved one is invisible (and
        # noisy). A failed task is reported even when it failed after close —
        # its failure must not disappear.
        error = task.exception()
        if error is not None:
            self.record_failure(f"task {task.get_name()}", error)

    # --------------------------------------------------------------- teardown

    async def aclose(self) -> None:
        """Close the scope exactly once.

        Safe under concurrent callers: the first caller creates the close task,
        every caller awaits the same task. Cancelling a caller does not abort an
        in-progress close.
        """

        close_task = self._close_task
        if close_task is None:
            if self._state is ScopeState.CLOSED:
                return
            close_task = asyncio.ensure_future(self._close())
            self._close_task = close_task
        await asyncio.shield(close_task)

    async def _close(self) -> None:
        self._state = ScopeState.CLOSING
        unwind_error: BaseException | None = None
        try:
            await self._stop_tasks()
            try:
                await self._stack.aclose()
            except BaseException as error:
                # A disposer raised something the wrapper does not swallow (or
                # the close itself was cancelled). AsyncExitStack has already
                # continued the unwind through the remaining disposers; record
                # the error and aggregate below instead of losing the other
                # failures or the original error.
                unwind_error = error
                self.record_failure("effect unwind", error)
        finally:
            self._state = ScopeState.CLOSED
        if self._failures:
            # Failures stay recorded after close: the scope is not fully
            # disposed, and diagnostics keep naming what failed.
            failures = tuple(self._failures)
            aggregated = EffectCleanupError(self._name, failures)
            if unwind_error is not None:
                raise aggregated from unwind_error
            raise aggregated

    async def _stop_tasks(self) -> None:
        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        if not pending:
            return
        _done, still_running = await asyncio.wait(pending, timeout=self._task_shutdown_timeout)
        self._stragglers = tuple(sorted(still_running, key=lambda task: task.get_name()))
        for task in self._stragglers:
            self.record_failure(
                f"task {task.get_name()}",
                TimeoutError(
                    f"task did not stop within {self._task_shutdown_timeout}s of scope close"
                ),
            )

    def record_failure(self, description: str, error: BaseException) -> None:
        """Record a teardown failure.

        Called by effect disposers and task supervisors so that a failing
        disposer never aborts the unwind. All recorded failures are aggregated
        into an :class:`~chassis.core.errors.EffectCleanupError` raised by
        :meth:`aclose`.
        """

        self._failures.append(CleanupFailure(description=description, error=error))

    def _release_effect(self, effect_id: str) -> None:
        self._effects.pop(effect_id, None)

    # -------------------------------------------------------- context manager

    async def __aenter__(self) -> Scope:
        self.assert_open("enter scope")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        await self.aclose()
        return False
