"""AgentSpec materialization is transactional and owner-safe (roadmap R010).

A failed materialization restores the exact previous scope and registry state,
a user-owned scope is never taken over, reserved metadata cannot collide, and
withdrawal removes only the state a revision owns.
"""

from __future__ import annotations

import pytest

from chassis import Harness, PluginContext, plugin
from chassis.agent_spec import AgentSpec
from chassis.core.errors import ConfigurationError


def build_plugin(name: str):  # type: ignore[no-untyped-def]
    @plugin(name=name, version="1.0.0")
    async def provide(ctx: PluginContext) -> None:
        return None

    return provide


def harness() -> Harness:
    instance = Harness(name="spec")
    instance.register_plugin_type("alpha", build_plugin("alpha"))
    instance.register_plugin_type("alpha-old", build_plugin("alpha-old"))
    instance.register_plugin_type("zeta", build_plugin("zeta"))
    instance.register_plugin_type("user", build_plugin("user"))
    return instance


async def test_install_refuses_to_take_over_an_existing_user_scope() -> None:
    h = harness()
    h.composition.child("shared", metadata={"user": "kept"})
    h.install(build_plugin("user"), entry_id="mine", scope="/shared")

    with pytest.raises(ConfigurationError) as excinfo:
        h.agents.install(AgentSpec(name="probe", revision="1", scope="/shared", plugins={}))

    assert excinfo.value.context["scope"] == "/shared"
    assert h.entry("mine") is not None
    scope = h.composition.get("/shared")
    assert scope is not None
    assert scope.metadata["user"] == "kept"
    assert h.agents.active_spec("probe") is None


def test_reserved_agent_metadata_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        AgentSpec(name="probe", revision="1", metadata={"chassis.agent": "spoof"})
    with pytest.raises(ConfigurationError):
        AgentSpec(name="probe", revision="1", metadata={"chassis.agent_revision": "99"})


async def test_a_mid_materialization_catalog_error_changes_nothing() -> None:
    h = harness()
    h.agents.install(AgentSpec(name="probe", revision="1", plugins={"alpha": {}}))
    before = h.agents.active_spec("probe")
    entry = h.entry("agent:probe:alpha")
    assert entry is not None

    with pytest.raises(ConfigurationError):
        h.agents.replace(
            AgentSpec(
                name="probe",
                revision="2",
                plugins={"alpha": {"x": 1}, "missing": {}},
            )
        )

    assert h.agents.active_spec("probe") is before
    restored = h.entry("agent:probe:alpha")
    assert restored is not None
    assert dict(restored.config) == {}
    assert h.agents.revisions("probe") == ("1",)


async def test_a_mid_materialization_failure_rolls_back_new_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = harness()
    real_install = Harness.install
    calls = {"count": 0}

    def failing_install(self: Harness, plugin_arg: object, **kwargs: object) -> str:
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("install exploded")
        return real_install(self, plugin_arg, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Harness, "install", failing_install)

    with pytest.raises(RuntimeError):
        h.agents.install(AgentSpec(name="probe", revision="1", plugins={"alpha": {}, "zeta": {}}))

    assert h.entry("agent:probe:alpha") is None
    assert h.composition.get("/agents/probe") is None
    assert h.agents.active_spec("probe") is None


async def test_a_replacement_failure_restores_the_replaced_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = harness()
    h.agents.install(AgentSpec(name="probe", revision="1", plugins={"alpha": {"v": 1}}))
    real_install = Harness.install
    calls = {"count": 0}

    def failing_install(self: Harness, plugin_arg: object, **kwargs: object) -> str:
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("install exploded")
        return real_install(self, plugin_arg, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Harness, "install", failing_install)

    replacement = build_plugin("alpha-old")
    with pytest.raises(RuntimeError):
        h.agents.replace(
            AgentSpec(name="probe", revision="2", plugins={"alpha": replacement, "zeta": {}})
        )

    restored = h.entry("agent:probe:alpha")
    assert restored is not None
    assert restored.manifest.name == "alpha"
    assert dict(restored.config) == {"v": 1}
    active = h.agents.active_spec("probe")
    assert active is not None and active.revision == "1"


async def test_a_replacement_failure_preserves_the_previous_revision() -> None:
    h = harness()
    h.agents.install(AgentSpec(name="probe", revision="1", plugins={"alpha": {}}))
    before = h.agents.active_spec("probe")

    with pytest.raises(ConfigurationError):
        h.agents.replace(
            AgentSpec(
                name="probe",
                revision="2",
                scope="/agents/probe-next",
                plugins={"missing": {}},
            )
        )

    after = h.agents.active_spec("probe")
    assert after is before and after is not None and after.revision == "1"
    assert h.composition.get("/agents/probe") is not None
    assert h.entry("agent:probe:alpha") is not None
    assert h.composition.get("/agents/probe-next") is None


async def test_withdrawal_removes_only_the_revisions_own_state() -> None:
    h = harness()
    h.composition.child("user")
    h.install(build_plugin("user"), entry_id="user-entry", scope="/user")
    h.agents.install(AgentSpec(name="probe", revision="1", plugins={"alpha": {}}))

    assert h.agents.remove("probe") is True

    assert h.entry("agent:probe:alpha") is None
    assert h.composition.get("/agents/probe") is None
    assert h.entry("user-entry") is not None
    assert h.composition.get("/user") is not None


async def test_old_and_new_revisions_keep_their_own_state() -> None:
    h = harness()
    h.agents.install(AgentSpec(name="probe", revision="1", plugins={"alpha": {}}))
    first = h.agents.spec("probe", "1")

    h.agents.replace(AgentSpec(name="probe", revision="2", plugins={"zeta": {}}))
    second = h.agents.spec("probe", "2")

    assert h.agents.active_spec("probe") is second
    assert first.entries == ("agent:probe:alpha",)
    assert second.entries == ("agent:probe:zeta",)
    assert h.entry("agent:probe:alpha") is None
    assert h.entry("agent:probe:zeta") is not None
    assert h.agents.revisions("probe") == ("1", "2")
