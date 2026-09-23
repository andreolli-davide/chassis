"""Scope-owned hook registry.

A hook registration belongs to the scope that created it, so closing that scope
removes the hook automatically: plugins never write matching deregistration code,
and a hook cannot outlive its owner.

Ordering is deterministic -- priority first, then registration order -- because
hook chains that depend on dictionary or import order are impossible to reason
about. Exception semantics are explicit per registration: a handler either fails
loudly or is recorded and the chain continues.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from chassis.core.collections import frozen_mapping
from chassis.core.errors import CleanupFailure, HookExecutionError
from chassis.core.scope import Scope
from chassis.hooks.types import (
    HookErrorPolicy,
    HookEvent,
    HookHandler,
    HookMode,
    HookRegistration,
    HookResult,
)

__all__ = ["HookRegistry", "HookSnapshot", "ScopedHooks"]


class HookSnapshot:
    """Immutable view of the hooks owned by one runtime generation.

    Dispatch against a snapshot means a run observes exactly the hooks of the
    generation it acquired: a plugin that left the composition stops contributing
    hooks to new runs without disturbing runs already in flight.
    """

    __slots__ = ("_generation_id", "_registrations")

    def __init__(self, generation_id: str, registrations: Iterable[HookRegistration]) -> None:
        self._generation_id = generation_id
        self._registrations = tuple(registrations)

    @property
    def generation_id(self) -> str:
        return self._generation_id

    @property
    def registrations(self) -> tuple[HookRegistration, ...]:
        return self._registrations

    def handlers(self, event: HookEvent) -> tuple[HookRegistration, ...]:
        return tuple(
            sorted(
                (item for item in self._registrations if item.event is event),
                key=lambda item: item.order_key,
            )
        )

    def __len__(self) -> int:
        return len(self._registrations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation_id": self._generation_id,
            "hooks": [item.to_dict() for item in self._registrations],
        }


class HookRegistry:
    """Live registry of hook handlers, with scope-owned registrations."""

    def __init__(self) -> None:
        self._registrations: dict[str, HookRegistration] = {}
        self._sequence = 0

    # ------------------------------------------------------------- registration

    def register(
        self,
        *,
        scope: Scope,
        event: HookEvent,
        handler: HookHandler,
        mode: HookMode = HookMode.OBSERVE,
        priority: int = 0,
        error_policy: HookErrorPolicy = HookErrorPolicy.RECORD,
        owner_id: str = "",
    ) -> HookRegistration:
        """Register ``handler`` for ``event``, owned by ``scope``."""

        scope.assert_open(f"register hook for {event.value}")
        self._sequence += 1
        registration = HookRegistration(
            registration_id=f"hook_{uuid.uuid4().hex[:12]}",
            event=event,
            handler=handler,
            handler_name=getattr(handler, "__qualname__", type(handler).__name__),
            mode=mode,
            priority=priority,
            error_policy=error_policy,
            sequence=self._sequence,
            owner_id=owner_id,
            scope_id=scope.id,
        )
        self._registrations[registration.registration_id] = registration
        scope.cleanup(
            f"hook {event.value} ({registration.handler_name})",
            self.unregister,
            registration.registration_id,
            kind="hook",
        )
        return registration

    def unregister(self, registration_id: str) -> bool:
        """Remove a registration. Returns whether it was present."""

        return self._registrations.pop(registration_id, None) is not None

    # ------------------------------------------------------------------ reading

    def registrations(self) -> tuple[HookRegistration, ...]:
        """All registrations in deterministic order."""

        return tuple(
            sorted(
                self._registrations.values(),
                key=lambda item: (item.event.value, item.order_key),
            )
        )

    def handlers(self, event: HookEvent) -> tuple[HookRegistration, ...]:
        """Ordered handlers for one event."""

        return tuple(
            sorted(
                (item for item in self._registrations.values() if item.event is event),
                key=lambda item: item.order_key,
            )
        )

    def __len__(self) -> int:
        return len(self._registrations)

    def snapshot(
        self, generation_id: str, *, owner_ids: Iterable[str] | None = None
    ) -> HookSnapshot:
        """Immutable view, optionally restricted to hooks owned by given instances.

        An empty ``owner_ids`` iterable yields an empty snapshot; ``None`` means
        "every registration".
        """

        if owner_ids is None:
            selected = self.registrations()
        else:
            owners = set(owner_ids)
            selected = tuple(item for item in self.registrations() if item.owner_id in owners)
        return HookSnapshot(generation_id, selected)

    def to_dict(self) -> dict[str, Any]:
        return {"hooks": [item.to_dict() for item in self.registrations()]}

    # ----------------------------------------------------------------- dispatch

    async def dispatch(
        self,
        event: HookEvent,
        payload: Mapping[str, Any] | None = None,
        *,
        hooks: HookSnapshot | None = None,
    ) -> HookResult:
        """Run the handler chain for ``event``.

        Args:
            event: Event being dispatched.
            payload: Initial payload.
            hooks: Immutable snapshot to dispatch against. ``None`` uses the live
                registry, which is appropriate for control-plane events.

        Raises:
            HookExecutionError: a handler registered with the ``RAISE`` error
                policy failed.
        """

        registrations = self.handlers(event) if hooks is None else hooks.handlers(event)
        current: Mapping[str, Any] = frozen_mapping(payload)
        failures = []
        stopped = False

        for registration in registrations:
            try:
                result = await registration.handler(current)
            except Exception as error:
                if registration.error_policy is HookErrorPolicy.RAISE:
                    raise HookExecutionError(
                        f"hook {registration.handler_name} failed for {event.value}",
                        event=event.value,
                        handler=registration.handler_name,
                        owner=registration.owner_id,
                    ) from error
                failures.append(
                    CleanupFailure(
                        description=f"hook {event.value} ({registration.handler_name})",
                        error=error,
                    )
                )
                continue

            if registration.mode is HookMode.OBSERVE or result is None:
                continue
            if registration.mode is HookMode.TRANSFORM:
                if not isinstance(result, Mapping):
                    error = TypeError("transform hook did not return a mapping")
                    if registration.error_policy is HookErrorPolicy.RAISE:
                        raise HookExecutionError(
                            f"transform hook {registration.handler_name} did not return a mapping",
                            event=event.value,
                            handler=registration.handler_name,
                            owner=registration.owner_id,
                        ) from error
                    failures.append(
                        CleanupFailure(
                            description=f"hook {event.value} ({registration.handler_name})",
                            error=error,
                        )
                    )
                    continue
                # TRANSFORM replaces the payload, exactly as documented: keys the
                # mapping does not carry are removed, and the replacement is
                # deep-frozen like the initial payload.
                current = frozen_mapping(result)
                continue
            if registration.mode is HookMode.BAIL and result:
                stopped = True
                break

        return HookResult(event=event, payload=current, stopped=stopped, failures=tuple(failures))


class ScopedHooks:
    """Plugin-facing facade bound to one plugin instance scope."""

    __slots__ = ("_owner_id", "_registry", "_scope")

    def __init__(self, registry: HookRegistry, scope: Scope, owner_id: str) -> None:
        self._registry = registry
        self._scope = scope
        self._owner_id = owner_id

    def register(
        self,
        event: HookEvent,
        handler: HookHandler,
        *,
        mode: HookMode = HookMode.OBSERVE,
        priority: int = 0,
        error_policy: HookErrorPolicy = HookErrorPolicy.RECORD,
    ) -> HookRegistration:
        """Register a handler for the lifetime of the plugin scope."""

        return self._registry.register(
            scope=self._scope,
            event=event,
            handler=handler,
            mode=mode,
            priority=priority,
            error_policy=error_policy,
            owner_id=self._owner_id,
        )

    def unregister(self, registration_id: str) -> bool:
        """Remove a registration early, before the scope closes."""

        return self._registry.unregister(registration_id)

    @property
    def registrations(self) -> tuple[HookRegistration, ...]:
        """Hook registrations this plugin currently owns."""

        return tuple(
            item for item in self._registry.registrations() if item.owner_id == self._owner_id
        )
