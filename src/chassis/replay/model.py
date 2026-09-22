"""The model boundary.

Chassis does not call models itself -- graphs do, through the ``MODEL``
capability -- so the model boundary is opt-in: wrap the model you provide and its
calls are recorded or replayed. Wrapping is deliberate, because a boundary the
harness does not mediate cannot be recorded honestly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from chassis._optional import EXTRA_LANGGRAPH, require_extra

require_extra(
    EXTRA_LANGGRAPH,
    "langchain_core",
    purpose="the replayable model boundary (ReplayChatModel)",
)

from langchain_core.callbacks import (  # noqa: E402
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, BaseMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from pydantic import ConfigDict  # noqa: E402

from chassis.core.errors import ConfigurationError, ReplayMismatch  # noqa: E402
from chassis.replay.models import BoundaryKind, ReplayFallback  # noqa: E402
from chassis.replay.session import ReplaySession, boundary_key  # noqa: E402

__all__ = ["ReplayChatModel"]


class ReplayChatModel(BaseChatModel):
    """Chat model that records its calls, or answers them from a recording.

    Args:
        session: Session owning the mode and the records.
        inner: Model used in live and record modes. May be ``None`` when replaying
            a complete recording, since nothing is delegated.
        model_name: Stable identity of the wrapped model, used in the boundary key
            so a recording cannot be replayed against a different model.
    """

    inner: BaseChatModel | None = None
    session: ReplaySession
    model_name: str = "replay-model"

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def _llm_type(self) -> str:
        return "chassis-replay"

    def _key(
        self, messages: Sequence[BaseMessage], stop: Sequence[str] | None, kwargs: Mapping[str, Any]
    ) -> str:
        return boundary_key(
            BoundaryKind.MODEL.value,
            self.model_name,
            [message.model_dump(mode="json") for message in messages],
            _canonical_options(stop, kwargs),
        )

    def _request(
        self, messages: Sequence[BaseMessage], stop: Sequence[str] | None, kwargs: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "model": self.model_name,
            "messages": [message.model_dump(mode="json") for message in messages],
            "options": _canonical_options(stop, kwargs),
        }

    def _recorded(
        self,
        key: str,
        messages: Sequence[BaseMessage],
        stop: Sequence[str] | None,
        kwargs: Mapping[str, Any],
        result: ChatResult,
    ) -> None:
        self.session.record(
            BoundaryKind.MODEL,
            key=key,
            request=self._request(messages, stop, kwargs),
            response=_serialize(result),
        )

    def _from_record(self, payload: Any) -> ChatResult:
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage.model_validate(generation["message"]),
                    generation_info=generation.get("generation_info"),
                )
                for generation in payload["generations"]
            ],
            llm_output=payload.get("llm_output"),
        )

    def _before_call(
        self, messages: Sequence[BaseMessage], stop: Sequence[str] | None, kwargs: Mapping[str, Any]
    ) -> tuple[str, ChatResult | None]:
        """Return the boundary key and, when replaying, the recorded result."""

        key = self._key(messages, stop, kwargs)
        if not self.session.is_replaying:
            return key, None
        if self.session.has_remaining(BoundaryKind.MODEL, key=key):
            return key, self._from_record(self.session.replay(BoundaryKind.MODEL, key=key).response)
        if self.session.fallback is ReplayFallback.ERROR:
            exhausted = self.session.has(BoundaryKind.MODEL, key=key)
            raise ReplayMismatch(
                "recorded model interactions for this key are exhausted"
                if exhausted
                else "model call was not recorded and replay does not fall back to live execution",
                model=self.model_name,
                reason="exhausted" if exhausted else "missing",
                recorded=self.session.counts().get(BoundaryKind.MODEL.value, 0),
            )
        return key, None

    def _require_inner(self) -> BaseChatModel:
        if self.inner is None:
            raise ReplayMismatch(
                "no model is available to execute this call live",
                model=self.model_name,
            )
        return self.inner

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        key, replayed = self._before_call(messages, stop, kwargs)
        if replayed is not None:
            return replayed
        result = self._require_inner()._generate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )
        self._recorded(key, messages, stop, kwargs, result)
        return result

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        key, replayed = self._before_call(messages, stop, kwargs)
        if replayed is not None:
            return replayed
        result = await self._require_inner()._agenerate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )
        self._recorded(key, messages, stop, kwargs, result)
        return result


def _serialize(result: ChatResult) -> dict[str, Any]:
    return {
        "generations": [
            {
                "message": generation.message.model_dump(mode="json"),
                "generation_info": generation.generation_info,
            }
            for generation in result.generations
        ],
        "llm_output": result.llm_output,
    }


def _canonical_options(stop: Sequence[str] | None, kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical, complete invocation options for a model boundary key.

    Stop sequences are normalized as a set (their order is not semantic), and
    every invocation kwarg — temperature, tools, structured output, provider
    options — is included. A value that cannot be canonicalized
    deterministically is rejected: silently omitting it would let genuinely
    different requests collide on one key.
    """

    options: dict[str, Any] = {
        "stop": None if stop is None else sorted({str(item) for item in stop})
    }
    for name, value in kwargs.items():
        options[name] = _canonical_value(name, value)
    return dict(sorted(options.items()))


def _canonical_value(name: str, value: Any) -> Any:
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise ReplayMismatch(
            "invocation option is not a canonical number",
            option=name,
            value=value,
        )
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                # str(key) would silently merge {1: ..., "1": ...} onto one entry.
                raise ConfigurationError(
                    "invocation option requires string mapping keys",
                    option=name,
                    key_type=type(key).__name__,
                )
            normalized[key] = _canonical_value(name, item)
        return dict(sorted(normalized.items()))
    if isinstance(value, (list, tuple)):
        return [_canonical_value(name, item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(str(_canonical_value(name, item)) for item in value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _canonical_value(name, model_dump(mode="json"))
    raise ReplayMismatch(
        "invocation option cannot be canonicalized into a replay key",
        option=name,
        value_type=type(value).__name__,
    )
