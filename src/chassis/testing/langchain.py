"""Test doubles that are themselves ``langchain-core`` objects.

These live apart from :mod:`chassis.testing.fakes` because they subclass
``langchain-core`` types at import time. Importing :mod:`chassis.testing` therefore
does not import them; touching :attr:`FakeChatModel` or :func:`fake_tool` does, and
fails with an actionable missing-extra error when the ``langgraph`` extra is not
installed.

Everything here exists to test the LangGraph adapter without a model provider.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from chassis._optional import EXTRA_LANGGRAPH, require_extra

require_extra(
    EXTRA_LANGGRAPH,
    "langchain_core",
    purpose="the langchain-core test doubles in chassis.testing",
)

from langchain_core.callbacks import (  # noqa: E402
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage  # noqa: E402
from langchain_core.outputs import (  # noqa: E402
    ChatGeneration,
    ChatGenerationChunk,
    ChatResult,
)
from langchain_core.tools import BaseTool, StructuredTool  # noqa: E402
from pydantic import ConfigDict, Field, create_model  # noqa: E402

__all__ = ["FakeChatModel", "fake_tool"]


class FakeChatModel(BaseChatModel):
    """Chat model that replays scripted responses and records its calls.

    Responses may be strings or ``AIMessage`` objects (the latter is how a scripted
    model emits tool calls). The final response repeats once the script is
    exhausted, so a graph loop terminates deterministically.
    """

    model_name: str = "fake-model"
    responses: list[Any] = Field(default_factory=list)
    calls: list[list[BaseMessage]] = Field(default_factory=list)
    position: int = 0
    delay_seconds: float = 0.0

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def _llm_type(self) -> str:
        return "chassis-fake"

    def _next_message(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        if not self.responses:
            return AIMessage(content="ok")
        index = min(self.position, len(self.responses) - 1)
        self.position += 1
        entry = self.responses[index]
        if isinstance(entry, str):
            return AIMessage(content=entry)
        return entry

    async def _delay(self) -> None:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next_message(messages))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        await self._delay()
        return ChatResult(generations=[ChatGeneration(message=self._next_message(messages))])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        message = self._next_message(messages)
        content = message.content if isinstance(message.content, str) else str(message.content)
        await self._delay()
        for index in range(0, len(content), 8):
            yield ChatGenerationChunk(message=AIMessageChunk(content=content[index : index + 8]))


def fake_tool(
    name: str,
    *,
    result: Any = None,
    error: BaseException | None = None,
    delay_seconds: float = 0.0,
    parameters: Mapping[str, Any] | None = None,
    calls: list[tuple[str, dict[str, Any]]] | None = None,
) -> BaseTool:
    """Build a LangChain-compatible tool with scripted behaviour.

    Args:
        name: Tool name.
        result: Value returned on success.
        error: Exception raised instead of returning.
        delay_seconds: Sleep before answering, for timeout tests.
        parameters: Argument name to ``(annotation, default)`` or annotation. The
            schema must declare the arguments a test passes: LangChain validates
            tool input against it and drops undeclared keys.
        calls: List that records ``(name, kwargs)`` for each invocation.
    """

    schema = create_model(f"{name}_args", **(dict(parameters or {})))

    async def _run(**kwargs: Any) -> Any:
        if calls is not None:
            calls.append((name, kwargs))
        if delay_seconds:
            await asyncio.sleep(delay_seconds)
        if error is not None:
            raise error
        return result

    return StructuredTool(
        name=name,
        description=f"Fake tool {name}",
        args_schema=schema,
        coroutine=_run,
    )
