"""Chassis: a production-grade Python agent harness.

Chassis owns runtime composition and lifecycle -- plugins, capabilities, scoped
resources, reversible effects, immutable runtime generations -- and hands an
immutable view of that composition to an execution engine such as LangGraph.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("chassis")
except PackageNotFoundError:  # pragma: no cover - only when running from a source tree
    __version__ = "0.0.0"

__all__ = ["__version__"]
