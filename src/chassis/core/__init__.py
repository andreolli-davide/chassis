"""Core ownership primitives: scopes, effects, and the Chassis error model."""

from __future__ import annotations

from chassis.core.collections import FrozenDict
from chassis.core.errors import (
    ChassisError,
    CleanupFailure,
    EffectCleanupError,
    ScopeClosedError,
)
from chassis.core.scope import EffectRecord, Scope, ScopeState

__all__ = [
    "ChassisError",
    "CleanupFailure",
    "EffectCleanupError",
    "EffectRecord",
    "FrozenDict",
    "Scope",
    "ScopeClosedError",
    "ScopeState",
]
