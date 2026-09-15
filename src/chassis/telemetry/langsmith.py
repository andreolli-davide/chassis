"""LangSmith telemetry backend.

LangGraph and LangChain already trace model, tool, and graph execution natively;
Chassis adds spans for the operations upstream cannot see -- plugin mount,
dependency resolution, reconciliation, generation publication and draining, policy
decisions, budget exhaustion, graph compilation and cache hits.

Spans nest under the ambient LangSmith run when one exists, so a Chassis span
appears inside the trace of the run that caused it. Attributes are redacted before
they leave the process.

Tracing is optional and never load-bearing: when it is disabled the backend costs
nothing, and when the SDK raises the failure is logged and the caller continues
(ARCH §26: observability must not corrupt runtime semantics).
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from chassis.secrets.redaction import SecretRedactor
from chassis.telemetry.base import NoopSpan

__all__ = ["LangSmithSpan", "LangSmithTelemetry"]

_LOGGER = logging.getLogger("chassis.telemetry.langsmith")

_TRACING_ENV_VARS = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2")


class LangSmithSpan:
    """Span backed by a LangSmith run."""

    __slots__ = ("_run",)

    def __init__(self, run: Any) -> None:
        self._run = run

    def set_attribute(self, key: str, value: Any) -> None:
        self.set_attributes({key: value})

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        try:
            self._run.add_metadata(dict(attributes))
        except Exception:
            _LOGGER.debug("failed to attach metadata to a LangSmith run", exc_info=True)

    def record_error(self, error: BaseException) -> None:
        self.set_attributes({"chassis_error": f"{type(error).__name__}: {error}"})


class LangSmithTelemetry:
    """Telemetry that reports Chassis operations to LangSmith.

    Args:
        enabled: Whether tracing is active. Defaults to the LangSmith environment
            variables, so an application that already configured tracing gets
            Chassis spans for free.
        project_name: LangSmith project override.
        client: LangSmith client override.
        redactor: Redactor applied to every attribute before it is sent.
        raise_on_error: Raise tracing failures instead of logging them. Off by
            default: observability is not a single point of failure.
    """

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        project_name: str | None = None,
        client: Any | None = None,
        redactor: SecretRedactor | None = None,
        raise_on_error: bool = False,
    ) -> None:
        self._enabled = _tracing_enabled() if enabled is None else enabled
        self._project_name = project_name
        self._client = client
        self._redactor = redactor if redactor is not None else SecretRedactor()
        self._raise_on_error = raise_on_error
        # Internal seams: tests substitute a recording run factory and current-run
        # lookup so the backend can be verified without network access.
        if self._enabled:
            from langsmith import get_current_run_tree, trace

            self._trace: Callable[..., Any] = trace
            self._current_run: Callable[[], Any] = get_current_run_tree
        else:
            self._trace = None  # type: ignore[assignment]
            self._current_run = None  # type: ignore[assignment]

    @property
    def enabled(self) -> bool:
        return self._enabled

    @asynccontextmanager
    async def span(
        self, name: str, attributes: Mapping[str, Any] | None = None
    ) -> AsyncGenerator[Any]:
        """Open a LangSmith run for the duration of the block."""

        if not self._enabled or self._trace is None:
            yield NoopSpan()
            return

        metadata = self._redactor.redact_value(dict(attributes or {}))
        context: Any = None
        run: Any = None
        try:
            context = self._trace(
                name,
                run_type="chain",
                metadata=metadata,
                tags=["chassis"],
                project_name=self._project_name,
                client=self._client,
            )
            run = context.__enter__()
        except Exception:
            if self._raise_on_error:
                raise
            _LOGGER.warning("LangSmith span %r failed; continuing without it", name, exc_info=True)
            # Entering the span failed, so degrade to a no-op span rather than
            # aborting the operation being traced.
            yield NoopSpan()
            return

        try:
            yield LangSmithSpan(run)
        finally:
            try:
                context.__exit__(None, None, None)
            except Exception:
                if self._raise_on_error:
                    raise
                _LOGGER.debug("failed to finish LangSmith span %r", name, exc_info=True)

    def event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        """Attach a point-in-time signal to the ambient LangSmith run.

        Without an ambient run there is nothing to attach to, so the event is
        dropped rather than inventing a parentless trace.
        """

        if not self._enabled or self._current_run is None:
            return
        try:
            run = self._current_run()
            if run is None:
                return
            payload = self._redactor.redact_value(dict(attributes or {}))
            run.add_event({"name": name, **payload})
        except Exception:
            if self._raise_on_error:
                raise
            _LOGGER.debug("failed to record LangSmith event %r", name, exc_info=True)


def _tracing_enabled() -> bool:
    for name in _TRACING_ENV_VARS:
        value = os.environ.get(name)
        if value is not None and value.strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False
