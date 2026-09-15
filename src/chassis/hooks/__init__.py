"""Hooks: harness-owned boundaries with scope-owned registrations."""

from __future__ import annotations

from chassis.hooks.registry import HookRegistry, HookSnapshot
from chassis.hooks.types import (
    HookErrorPolicy,
    HookEvent,
    HookHandler,
    HookMode,
    HookPayload,
    HookRegistration,
    HookResult,
)

__all__ = [
    "HookErrorPolicy",
    "HookEvent",
    "HookHandler",
    "HookMode",
    "HookPayload",
    "HookRegistration",
    "HookRegistry",
    "HookResult",
    "HookSnapshot",
]
