"""``TestHarness``: a Chassis runtime wired for tests.

A plugin author should not need a production deployment to test lifecycle
behaviour. ``TestHarness`` is a real :class:`~chassis.harness.Harness` with
recording telemetry, an in-memory secret provider, and an explicit policy, plus
small helpers for the things tests do constantly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from langchain_core.tools import BaseTool

from chassis.capabilities.keys import TOOLS, CapabilityKey
from chassis.core.errors import ChassisError
from chassis.harness import Harness
from chassis.plugins.base import Plugin, PluginContext, plugin
from chassis.plugins.lifecycle import PluginInstance
from chassis.policy.engine import PolicyEngine
from chassis.runtime import RunEnvironment
from chassis.secrets.base import SecretProvider
from chassis.telemetry.recording import RecordingTelemetry
from chassis.testing.fakes import FakePolicy, FakeSecrets, FakeTelemetry
from chassis.tools.metadata import ToolPolicy
from chassis.tools.registry import ToolSnapshot

if TYPE_CHECKING:
    from chassis.langgraph.graphs import AgentDefinition, GraphCache
    from chassis.langgraph.runtime import LangGraphAgent

__all__ = ["TestHarness"]


class TestHarness(Harness):
    """A harness preconfigured with deterministic doubles.

    Not collected by pytest as a test class.

    Args:
        policy: Policy engine. Defaults to a recording grant policy with no grants,
            so permission-requiring tools are denied unless a test grants them.
        secrets: Secret provider. Defaults to an empty in-memory provider.
        telemetry: Telemetry sink. Defaults to a recording backend.
        tools: Tools to register through a toolbox plugin at ``start``.
        plugins: Plugin classes or instances to install.
        kwargs: Forwarded to :class:`~chassis.harness.Harness`.
    """

    #: Kept in the public API under its documented name without being collected
    #: as a pytest test class.
    __test__ = False

    def __init__(
        self,
        *,
        policy: PolicyEngine | None = None,
        secrets: SecretProvider | None = None,
        telemetry: RecordingTelemetry | None = None,
        tools: Sequence[BaseTool] = (),
        tool_policies: Mapping[str, ToolPolicy] | None = None,
        plugins: Sequence[Plugin | type[Plugin]] = (),
        **kwargs: Any,
    ) -> None:
        resolved_telemetry = telemetry if telemetry is not None else FakeTelemetry()
        super().__init__(
            name=kwargs.pop("name", "test-harness"),
            policy=policy if policy is not None else FakePolicy(),
            secrets=secrets if secrets is not None else FakeSecrets(),
            telemetry=resolved_telemetry,
            **kwargs,
        )
        self._recording = resolved_telemetry
        for index, plugin_type in enumerate(plugins):
            self.install(plugin_type, entry_id=f"plugin-{index + 1}")
        if tools:
            self.install_tools(*tools, policies=tool_policies)

    # ------------------------------------------------------------------ helpers

    @property
    def telemetry(self) -> RecordingTelemetry:  # type: ignore[override]
        """The recording telemetry sink, for assertions."""

        return self._recording

    @property
    def recorded_spans(self) -> list[str]:
        return self._recording.span_names()

    @property
    def recorded_events(self) -> list[str]:
        return self._recording.event_names()

    def install_tools(
        self,
        *tools: BaseTool,
        policies: Mapping[str, ToolPolicy] | None = None,
        entry_id: str = "toolbox",
    ) -> str:
        """Register tools through a plugin so they are scope-owned like any other."""

        mapping = dict(policies or {})

        @plugin(name="chassis-toolbox", version="1.0.0", provides={"tools": "1"})
        async def toolbox(ctx: PluginContext) -> None:
            for tool in tools:
                ctx.tools.register(tool, policy=mapping.get(tool.name))
            ctx.capabilities.provide(TOOLS, tuple(item.name for item in tools))

        return self.install(toolbox, entry_id=entry_id, replace=entry_id in self._entry_ids())

    def agent(
        self,
        definition: AgentDefinition,
        *,
        cache: GraphCache | None = None,
        checkpointer: Any | None = None,
        store: Any | None = None,
        stream_mode: Any = "values",
    ) -> LangGraphAgent:
        """Build a LangGraph agent wired to this harness's telemetry and redaction.

        The harness cannot construct one itself: the core layer must not depend on
        LangGraph, and neither does this package at import time. Test code should
        not have to thread those services through by hand, so the convenience lives
        here.
        """

        from chassis.langgraph.runtime import LangGraphAgent

        return LangGraphAgent(
            definition,
            cache=cache,
            checkpointer=checkpointer,
            store=store,
            telemetry=self.telemetry,
            redactor=self.redactor,
            stream_mode=stream_mode,
        )

    def instance(self, entry_id: str) -> PluginInstance:
        """Return a mounted instance, failing the test when it is absent."""

        instance = self.plugin_registry.instance(entry_id)
        assert instance is not None, f"entry {entry_id!r} is not mounted"
        return instance

    def tools_of(self) -> ToolSnapshot:
        """The tool snapshot of the current generation."""

        generation = self.current_generation
        assert generation is not None, "no generation is published"
        return self.tool_snapshot(generation)

    def environment(self, *, limits: Any | None = None) -> RunEnvironment:
        """The run environment of the current generation."""

        generation = self.current_generation
        assert generation is not None, "no generation is published"
        return self.run_environment(generation, limits=limits)

    def provide_secret(self, name: str, value: str) -> None:
        """Add a secret to the in-memory provider."""

        provider = self.secrets
        inner = getattr(provider, "inner", provider)
        if not isinstance(inner, FakeSecrets):  # pragma: no cover - custom provider
            raise ChassisError("the configured secret provider is not writable")
        inner.add(name, value)

    def capability_providers(self, capability: CapabilityKey | str) -> tuple[str, ...]:
        """Names of the plugins currently providing a capability."""

        name = capability.name if isinstance(capability, CapabilityKey) else capability
        return tuple(
            registration.provider_name for registration in self.capability_registry.by_name(name)
        )

    def _entry_ids(self) -> set[str]:
        return {entry.entry_id for entry in self.plugin_registry.entries()}
