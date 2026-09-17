"""Fakes for testing plugins without a deployment.

These are deliberately small and deterministic, and they import nothing beyond the
Chassis core. Where a production implementation already behaves correctly in memory
(``GrantPolicy``, ``StaticSecretProvider``, ``RecordingTelemetry``), the fake
extends it and adds recording rather than reimplementing it, so tests exercise the
same logic production uses.

LangChain-specific doubles (a scripted chat model and a scripted tool) live in
:mod:`chassis.testing.langchain` and are reached through :mod:`chassis.testing`,
which imports them lazily.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from chassis.policy.engine import GrantPolicy, PolicyRequest, PolicyResult
from chassis.secrets.env import StaticSecretProvider
from chassis.telemetry.recording import RecordingTelemetry

__all__ = [
    "FakePolicy",
    "FakeSecrets",
    "FakeTelemetry",
]


class FakePolicy(GrantPolicy):
    """Grant policy that records every decision it is asked for."""

    def __init__(
        self,
        grants: Sequence[str] = (),
        *,
        approval_required: Sequence[str] = (),
        deny: Sequence[str] = (),
    ) -> None:
        super().__init__(grants, approval_required=approval_required)
        self.denied = frozenset(deny)
        self.requests: list[PolicyRequest] = []
        self.results: list[PolicyResult] = []

    async def evaluate(self, request: PolicyRequest) -> PolicyResult:
        self.requests.append(request)
        if request.permission.name in self.denied:
            result = PolicyResult(
                allowed=False, reason=f"denied by test policy: {request.permission}"
            )
        else:
            result = await super().evaluate(request)
        self.results.append(result)
        return result

    @property
    def denied_permissions(self) -> tuple[str, ...]:
        return tuple(
            str(item.permission)
            for item, result in zip(self.requests, self.results, strict=True)
            if not result.allowed
        )


class FakeSecrets(StaticSecretProvider):
    """In-memory secret provider labelled for tests."""

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        super().__init__(values, name="fake")


#: Telemetry that records everything it receives.
FakeTelemetry = RecordingTelemetry
