"""LangGraph execution behind the Chassis agent boundary.

``LangGraphAgent`` adapts one :class:`~chassis.langgraph.graphs.AgentDefinition`
to the :class:`~chassis.runtime.AgentRuntime` protocol. It owns graph compilation
and caching, checkpointing, streaming, and interrupt/resume; Chassis owns
composition, policy, budgets, and the run's coherent environment.

The graph is compiled once per distinct set of build-time inputs and reused for
every generation that shares them, so swapping a runtime-bound provider -- a model,
a database, a policy -- costs a new capability snapshot and nothing else.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StreamMode

from chassis.core.errors import GraphBuildError
from chassis.langgraph.graphs import (
    AgentDefinition,
    GraphBuildInputs,
    GraphCache,
    GraphCacheKey,
    build_cache_key,
)
from chassis.runtime import (
    AgentEvent,
    AgentInterrupt,
    AgentRequest,
    AgentResult,
    HarnessRunContext,
)
from chassis.secrets.redaction import SecretRedactor
from chassis.telemetry.base import NoopTelemetry, Telemetry
from chassis.tools.registry import ToolSnapshot

__all__ = ["LangGraphAgent"]


class LangGraphAgent:
    """Executes one agent definition on LangGraph.

    Args:
        definition: Agent definition (state schema, builder, build-time inputs).
        cache: Compiled-graph cache shared by agents that build the same graphs.
        checkpointer: LangGraph checkpointer. LangGraph owns durable graph state;
            Chassis never mirrors it.
        store: LangGraph Store for cross-thread data.
        telemetry: Instrumentation sink.
        redactor: Redactor applied to trace metadata before it leaves the process.
        stream_mode: Default stream mode(s) for :meth:`stream`.
    """

    def __init__(
        self,
        definition: AgentDefinition,
        *,
        cache: GraphCache | None = None,
        checkpointer: Any | None = None,
        store: Any | None = None,
        telemetry: Telemetry | None = None,
        redactor: SecretRedactor | None = None,
        stream_mode: StreamMode | Sequence[StreamMode] = "values",
    ) -> None:
        self._definition = definition
        self._cache = cache if cache is not None else GraphCache(telemetry=telemetry)
        self._checkpointer = checkpointer
        self._store = store
        self._telemetry = telemetry if telemetry is not None else NoopTelemetry()
        self._redactor = redactor if redactor is not None else SecretRedactor()
        self._stream_mode: list[StreamMode] = (
            [stream_mode] if isinstance(stream_mode, str) else list(stream_mode)
        )

    # ------------------------------------------------------------------ identity

    @property
    def name(self) -> str:
        return self._definition.name

    @property
    def definition(self) -> AgentDefinition:
        return self._definition

    @property
    def cache(self) -> GraphCache:
        return self._cache

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self._definition.identity,
            "runtime": "langgraph",
            "checkpointer": type(self._checkpointer).__name__ if self._checkpointer else None,
            "store": type(self._store).__name__ if self._store else None,
        }

    # -------------------------------------------------------------------- graphs

    def cache_key(self, run_context: HarnessRunContext) -> GraphCacheKey:
        """The build-time cache key this run would use."""

        return build_cache_key(
            self._definition,
            tools=self._static_tools(run_context),
            build_time_versions=self._build_time_versions(run_context),
        )

    def graph(self, run_context: HarnessRunContext) -> CompiledStateGraph[Any, Any, Any, Any]:
        """Return the compiled graph for this run, compiling it on a miss.

        Raises:
            GraphBuildError: the definition could not be built or compiled.
        """

        tools = self._static_tools(run_context)
        versions = self._build_time_versions(run_context)
        key = build_cache_key(self._definition, tools=tools, build_time_versions=versions)

        cached = self._cache.get(key)
        if cached is not None:
            return cached

        snapshot = run_context.tools
        inputs = GraphBuildInputs(
            agent=self._definition.name,
            definition_version=self._definition.version,
            tools=tools,
            tool_snapshot=snapshot if snapshot is not None else ToolSnapshot("", ()),
            build_time_versions=versions,
        )
        try:
            builder_graph = self._definition.build(inputs)
        except GraphBuildError:
            raise
        except Exception as error:
            raise GraphBuildError(
                f"agent {self._definition.name!r} failed to build its graph",
                agent=self._definition.name,
                definition_version=self._definition.version,
            ) from error

        try:
            compiled = builder_graph.compile(checkpointer=self._checkpointer, store=self._store)
        except Exception as error:
            raise GraphBuildError(
                f"agent {self._definition.name!r} failed to compile its graph",
                agent=self._definition.name,
                definition_version=self._definition.version,
            ) from error

        self._telemetry.event(
            "graph.compile",
            {
                "agent": self._definition.name,
                "definition_version": self._definition.version,
                "cache_key": key.digest(),
                "tools": [tool.name for tool in tools],
            },
        )
        self._cache.put(key, compiled)
        return compiled

    def _static_tools(self, run_context: HarnessRunContext) -> tuple[BaseTool, ...]:
        """The langchain-core tools compiled into the graph.

        The registry wraps tools with harness metadata; the graph must receive the
        underlying tools, never the wrappers.
        """

        snapshot = run_context.tools
        if snapshot is None:
            return ()
        return tuple(entry.tool for entry in snapshot.entries)

    def _build_time_versions(self, run_context: HarnessRunContext) -> dict[str, str]:
        versions: dict[str, str] = {}
        for capability in self._definition.build_time_capabilities:
            providers = run_context.capabilities.providers(capability)
            versions[capability] = (
                ",".join(sorted(str(provider.version) for provider in providers))
                if providers
                else "absent"
            )
        return versions

    # --------------------------------------------------------------- execution

    async def invoke(self, request: AgentRequest, run_context: HarnessRunContext) -> AgentResult:
        """Execute the graph once and normalize the result."""

        graph = self.graph(run_context)
        started = time.monotonic()
        raw = await graph.ainvoke(
            self._payload(request), config=self._config(request, run_context), context=run_context
        )
        return self._result(raw, request, run_context, time.monotonic() - started)

    async def stream(
        self, request: AgentRequest, run_context: HarnessRunContext
    ) -> AsyncIterator[AgentEvent]:
        """Execute the graph, emitting events as LangGraph produces them."""

        graph = self.graph(run_context)
        async for chunk in graph.astream(
            self._payload(request),
            config=self._config(request, run_context),
            context=run_context,
            stream_mode=self._stream_mode[0] if len(self._stream_mode) == 1 else self._stream_mode,
        ):
            kind, data = self._split_chunk(chunk)
            yield AgentEvent(
                agent=self._definition.name,
                generation_id=run_context.generation_id,
                run_id=run_context.run_id,
                kind=kind,
                data=data,
            )

    # --------------------------------------------------------------- internals

    @staticmethod
    def _payload(request: AgentRequest) -> Any:
        if request.is_resume:
            return Command(resume=request.resume)
        return request.input

    def _config(self, request: AgentRequest, run_context: HarnessRunContext) -> RunnableConfig:
        configurable: dict[str, Any] = {}
        if request.thread_id:
            configurable["thread_id"] = request.thread_id
        if request.checkpoint_id:
            configurable["checkpoint_id"] = request.checkpoint_id
        metadata = self._redactor.redact_value(
            {
                "chassis_agent": self._definition.name,
                "chassis_agent_version": self._definition.version,
                "chassis_generation_id": run_context.generation_id,
                "chassis_run_id": run_context.run_id,
                "chassis_user_id": run_context.user_id,
                "chassis_tenant_id": run_context.tenant_id,
                **dict(request.metadata),
            }
        )
        return {
            "configurable": configurable,
            "metadata": metadata,
            "tags": [f"chassis:agent:{self._definition.name}"],
        }

    def _result(
        self,
        raw: Any,
        request: AgentRequest,
        run_context: HarnessRunContext,
        duration: float,
    ) -> AgentResult:
        return AgentResult(
            agent=self._definition.name,
            generation_id=run_context.generation_id,
            run_id=run_context.run_id,
            output=raw,
            thread_id=request.thread_id,
            interrupts=_interrupts(raw),
            duration_seconds=duration,
            metadata={
                "agent_version": self._definition.version,
                "runtime": "langgraph",
                **dict(run_context.metadata),
            },
        )

    def _split_chunk(self, chunk: Any) -> tuple[str, Any]:
        if len(self._stream_mode) > 1:
            mode, data = chunk
            return str(mode), data
        return str(self._stream_mode[0]), chunk


def _interrupts(raw: Any) -> tuple[AgentInterrupt, ...]:
    if not isinstance(raw, Mapping):
        return ()
    values = raw.get("__interrupt__")
    if not values:
        return ()
    interrupts: list[AgentInterrupt] = []
    for item in values:
        identifier = getattr(item, "id", None)
        interrupts.append(
            AgentInterrupt(
                value=getattr(item, "value", item),
                interrupt_id=str(identifier) if identifier else None,
            )
        )
    return tuple(interrupts)
