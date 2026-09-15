from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager

import pytest

from chassis.core.errors import EffectCleanupError, ScopeClosedError
from chassis.core.scope import Scope, ScopeState


async def test_scope_unwinds_effects_in_reverse_order() -> None:
    order: list[str] = []
    async with Scope("test") as scope:
        scope.cleanup("first", lambda: order.append("first"))
        scope.cleanup("second", lambda: order.append("second"))
        scope.cleanup("third", lambda: order.append("third"))

    assert order == ["third", "second", "first"]


async def test_scope_tracks_effect_records_and_releases_them() -> None:
    scope = Scope("test")
    record = scope.cleanup("undone", lambda: None)
    assert [effect.description for effect in scope.effects] == ["undone"]
    assert record.kind == "effect"
    assert record.scope_id == scope.id

    await scope.aclose()
    assert scope.effects == ()
    assert scope.state is ScopeState.CLOSED


async def test_scope_aggregates_cleanup_failures_and_continues() -> None:
    order: list[str] = []

    def failing() -> None:
        order.append("failing")
        raise RuntimeError("boom")

    scope = Scope("aggregate")
    scope.cleanup("outer", lambda: order.append("outer"))
    scope.cleanup("failing", failing)
    scope.cleanup("inner", lambda: order.append("inner"))

    with pytest.raises(EffectCleanupError) as excinfo:
        await scope.aclose()

    assert order == ["inner", "failing", "outer"]
    assert [failure.description for failure in excinfo.value.failures] == ["failing"]
    assert isinstance(excinfo.value.failures[0].error, RuntimeError)
    assert "RuntimeError" in str(excinfo.value)


async def test_scope_rejects_new_effects_after_close() -> None:
    scope = Scope("closed")
    await scope.aclose()

    with pytest.raises(ScopeClosedError):
        scope.cleanup("late", lambda: None)
    with pytest.raises(ScopeClosedError):
        scope.register_effect("late", "late")
    with pytest.raises(ScopeClosedError):
        scope.child("late")


async def test_scope_close_is_idempotent_and_concurrently_safe() -> None:
    closures = 0

    def on_close() -> None:
        nonlocal closures
        closures += 1

    scope = Scope("idempotent")
    scope.cleanup("count", on_close)

    await asyncio.gather(scope.aclose(), scope.aclose(), scope.aclose())
    await scope.aclose()

    assert closures == 1
    assert scope.state is ScopeState.CLOSED


async def test_scope_close_survives_caller_cancellation() -> None:
    cleaned: list[str] = []
    started = asyncio.Event()

    async def slow_cleanup() -> None:
        started.set()
        await asyncio.sleep(0.05)
        cleaned.append("done")

    scope = Scope("cancellable")
    scope.cleanup("slow", slow_cleanup)

    closer = asyncio.ensure_future(scope.aclose())
    await started.wait()
    closer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closer

    await scope.aclose()
    assert cleaned == ["done"]
    assert scope.is_closed


async def test_scope_cancels_owned_tasks_on_close() -> None:
    cancelled = asyncio.Event()

    async def forever() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    scope = Scope("tasks")
    task = scope.create_task(forever(), name="worker")
    await asyncio.sleep(0)
    assert scope.tasks == (task,)

    await scope.aclose()

    assert cancelled.is_set()
    assert task.cancelled()
    assert scope.tasks == ()


async def test_scope_rejects_task_creation_after_close() -> None:
    scope = Scope("tasks-closed")
    await scope.aclose()

    async def noop() -> None:
        return None

    coro = noop()
    with pytest.raises(ScopeClosedError):
        scope.create_task(coro)
    coro.close()


async def test_scope_surfaces_failed_owned_task() -> None:
    async def boom() -> None:
        raise ValueError("task exploded")

    scope = Scope("failing-task")
    task = scope.create_task(boom(), name="exploder")
    with pytest.raises(ValueError):
        await task
    await asyncio.sleep(0)

    with pytest.raises(EffectCleanupError) as excinfo:
        await scope.aclose()

    assert [failure.description for failure in excinfo.value.failures] == ["task exploder"]
    assert isinstance(excinfo.value.failures[0].error, ValueError)


async def test_scope_times_out_stubborn_tasks() -> None:
    stripped = asyncio.Event()

    async def stubborn() -> None:
        attempts = 0
        while True:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                attempts += 1
                stripped.set()
                if attempts >= 2:
                    raise

    scope = Scope("stubborn", task_shutdown_timeout=0.05)
    task = scope.create_task(stubborn(), name="stubborn")
    await asyncio.sleep(0)

    with pytest.raises(EffectCleanupError) as excinfo:
        await scope.aclose()

    assert stripped.is_set()
    assert [failure.description for failure in excinfo.value.failures] == ["task stubborn"]
    assert isinstance(excinfo.value.failures[0].error, TimeoutError)

    # A scope cannot force a task that refuses cancellation; the operator is told,
    # and the task still terminates once it accepts cancellation.
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_scope_unwinds_entered_context_managers() -> None:
    events: list[str] = []

    @contextmanager
    def sync_cm() -> Generator[str]:
        events.append("sync-enter")
        try:
            yield "sync-value"
        finally:
            events.append("sync-exit")

    @asynccontextmanager
    async def async_cm() -> AsyncGenerator[str]:
        events.append("async-enter")
        try:
            yield "async-value"
        finally:
            events.append("async-exit")

    async with Scope("contexts") as scope:
        assert scope.enter_context(sync_cm()) == "sync-value"
        assert await scope.enter_async_context(async_cm()) == "async-value"

    assert events == ["sync-enter", "async-enter", "async-exit", "sync-exit"]


async def test_scope_records_context_manager_exit_failure() -> None:
    @asynccontextmanager
    async def broken() -> AsyncGenerator[None]:
        yield None
        raise RuntimeError("exit failed")

    scope = Scope("broken-context")
    await scope.enter_async_context(broken())

    with pytest.raises(EffectCleanupError) as excinfo:
        await scope.aclose()

    assert [failure.description for failure in excinfo.value.failures] == [
        "async context _AsyncGeneratorContextManager"
    ]


async def test_child_scope_closes_before_parent_effects() -> None:
    order: list[str] = []
    scope = Scope("parent")
    scope.cleanup("parent-effect", lambda: order.append("parent"))
    child = scope.child("child")
    child.cleanup("child-effect", lambda: order.append("child"))

    await scope.aclose()

    assert order == ["child", "parent"]
    assert scope.children == (child,)
    assert child.is_closed


async def test_child_scope_owns_its_own_tasks() -> None:
    cancelled = asyncio.Event()

    async def forever() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    scope = Scope("parent")
    child = scope.child("child")
    child.create_task(forever(), name="child-worker")

    await scope.aclose()

    assert cancelled.is_set()


async def test_scope_diagnostics_redact_nothing_but_expose_ownership() -> None:
    scope = Scope("diagnostics", description="unit")
    child = scope.child("child")
    scope.cleanup("effect", lambda: None)

    payload = scope.to_dict()

    assert payload["name"] == "diagnostics"
    assert payload["state"] == "open"
    assert payload["parent"] is None
    assert payload["children"] == [child.id]
    descriptions = [effect["description"] for effect in payload["effects"]]
    assert descriptions == ["child scope 'child'", "effect"]

    await scope.aclose()
