"""The model boundary.

Chassis does not call models itself -- graphs do, through the ``MODEL``
capability -- so the model boundary is opt-in: wrap the model you provide and its
calls are recorded or replayed. Wrapping is deliberate, because a boundary the
harness does not mediate cannot be recorded honestly.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict

from chassis.core.errors import ReplayMismatch
from chassis.replay.models import BoundaryKind, ReplayFallback
from chassis.replay.session import ReplaySession, boundary_key

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

    def _key(self, messages: Sequence[BaseMessage]) -> str:
        return boundary_key(
            BoundaryKind.MODEL.value,
            self.model_name,
            [message.model_dump(mode="json") for message in messages],
        )

    def _request(self, messages: Sequence[BaseMessage]) -> dict[str, Any]:
        return {
            "model": self.model_name,
            "messages": [message.model_dump(mode="json") for message in messages],
        }

    def _recorded(self, key: str, messages: Sequence[BaseMessage], result: ChatResult) -> None:
        self.session.record(
            BoundaryKind.MODEL,
            key=key,
            request=self._request(messages),
            response=_serialize(result),
        )

    def _from_record(self, payload: Any) -> ChatResult:
        return ChatResult(
            generations=[
                ChatGeneration(message=AIMessage.model_validate(generation["message"]))
                for generation in payload["generations"]
            ]
        )

    def _before_call(self, messages: Sequence[BaseMessage]) -> tuple[str, ChatResult | None]:
        """Return the boundary key and, when replaying, the recorded result."""

        key = self._key(messages)
        if not self.session.is_replaying:
            return key, None
        if self.session.has(BoundaryKind.MODEL, key=key):
            return key, self._from_record(self.session.replay(BoundaryKind.MODEL, key=key).response)
        if self.session.fallback is ReplayFallback.ERROR:
            raise ReplayMismatch(
                "model call was not recorded and replay does not fall back to live execution",
                model=self.model_name,
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
        key, replayed = self._before_call(messages)
        if replayed is not None:
            return replayed
        result = self._require_inner()._generate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )
        self._recorded(key, messages, result)
        return result

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        key, replayed = self._before_call(messages)
        if replayed is not None:
            return replayed
        result = await self._require_inner()._agenerate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )
        self._recorded(key, messages, result)
        return result


def _serialize(result: ChatResult) -> dict[str, Any]:
    return {
        "generations": [
            {"message": generation.message.model_dump(mode="json")}
            for generation in result.generations
        ]
    }
