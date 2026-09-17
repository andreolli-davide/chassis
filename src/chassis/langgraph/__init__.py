"""LangGraph integration: the first-class execution engine behind AgentRuntime.

Chassis keeps LangGraph behind a small execution boundary. The core lifecycle and
generation kernel never import LangGraph; this package adapts agent definitions,
graph caching, streaming, interrupts, checkpointing, and harness-mediated tool
execution to it.
"""

from __future__ import annotations

from chassis._optional import EXTRA_LANGGRAPH, require_extra

require_extra(
    EXTRA_LANGGRAPH,
    "langchain_core",
    "langgraph",
    purpose="the LangGraph adapter (chassis.langgraph)",
)

from chassis.langgraph.graphs import (  # noqa: E402
    AgentDefinition,
    GraphBuildInputs,
    GraphCache,
    GraphCacheKey,
    GraphCacheStats,
    build_cache_key,
)
from chassis.langgraph.runtime import LangGraphAgent  # noqa: E402
from chassis.langgraph.tools import harness_tool_node  # noqa: E402

__all__ = [
    "AgentDefinition",
    "GraphBuildInputs",
    "GraphCache",
    "GraphCacheKey",
    "GraphCacheStats",
    "LangGraphAgent",
    "build_cache_key",
    "harness_tool_node",
]
