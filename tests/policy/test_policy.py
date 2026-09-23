from __future__ import annotations

from typing import Any

import pytest
from langchain_core.tools import tool

from chassis import Harness, PluginContext, plugin
from chassis.capabilities import POLICY, SECRETS
from chassis.core.errors import (
    ConfigurationError,
    PolicyDenied,
    SecretResolutionError,
)
from chassis.policy import (
    AllowAllPolicy,
    DenyAllPolicy,
    GrantPolicy,
    Permission,
    PermissionGrant,
    PolicyEngine,
    PolicyRequest,
    PolicyResult,
)
from chassis.secrets import StaticSecretProvider
from chassis.tools import ToolPolicy, ToolRequest


def test_permission_parsing_round_trips() -> None:
    assert Permission.parse("filesystem.read") == Permission("filesystem.read")
    assert Permission.parse("filesystem.write:/workspace/**") == Permission(
        "filesystem.write", "/workspace/**"
    )
    assert str(Permission.parse("filesystem.write:/out/**")) == "filesystem.write:/out/**"


@pytest.mark.parametrize("text", ["", ":resource", "name:"])
def test_invalid_permissions_are_rejected(text: str) -> None:
    with pytest.raises(ConfigurationError):
        Permission.parse(text)


def test_unscoped_grant_covers_every_resource() -> None:
    grant = PermissionGrant("filesystem.read")

    assert grant.allows(Permission("filesystem.read"))
    assert grant.allows(Permission("filesystem.read", "/tmp/anything"))
    assert not grant.allows(Permission("filesystem.write", "/tmp/anything"))
    assert not grant.allows(Permission("filesystem.readonly"))


def test_scoped_grant_matches_only_its_pattern() -> None:
    grant = PermissionGrant.parse("filesystem.write:/workspace/output/**")

    assert grant.allows(Permission("filesystem.write", "/workspace/output/report.txt"))
    assert not grant.allows(Permission("filesystem.write", "/etc/passwd"))
    # A scoped grant cannot satisfy an unscoped request.
    assert not grant.allows(Permission("filesystem.write"))


async def test_grant_policy_allows_only_granted_permissions() -> None:
    policy = GrantPolicy(["filesystem.read", "network.fetch"])

    allowed = await policy.evaluate(
        PolicyRequest(permission=Permission("network.fetch"), subject="t")
    )
    assert allowed.allowed
    assert allowed.matched_grant == PermissionGrant("network.fetch")

    denied = await policy.evaluate(
        PolicyRequest(permission=Permission("filesystem.delete", "/etc/passwd"), subject="t")
    )
    assert not denied.allowed
    assert "no grant covers filesystem.delete:/etc/passwd" in denied.reason


async def test_grant_policy_flags_permissions_that_need_approval() -> None:
    policy = GrantPolicy(["shell.execute"], approval_required=["shell.execute"])

    result = await policy.evaluate(
        PolicyRequest(permission=Permission("shell.execute"), subject="bash")
    )

    assert result.allowed
    assert result.approval_required is True
    assert "pending approval" in result.reason


async def test_default_and_deny_policies_are_explicit() -> None:
    request = PolicyRequest(permission=Permission("filesystem.delete"), subject="t")

    assert (await AllowAllPolicy().evaluate(request)).allowed is True
    assert (await DenyAllPolicy().evaluate(request)).allowed is False


async def test_policy_engines_satisfy_the_protocol() -> None:
    assert isinstance(GrantPolicy(), PolicyEngine)
    assert isinstance(AllowAllPolicy(), PolicyEngine)
    assert isinstance(DenyAllPolicy(), PolicyEngine)


# --------------------------------------------------------------------------
# System policy resolution (R002): resolution and provider failures deny.
# --------------------------------------------------------------------------

SECRET = "sk-live-abcdef123456"


@tool
def fetch(url: str) -> str:
    """Fetch a URL."""

    return "fetched"


class ExplodingPolicy:
    """Policy provider whose engine fails during evaluation."""

    async def evaluate(self, request: PolicyRequest) -> PolicyResult:
        raise RuntimeError(f"policy backend down: {SECRET}")


def policy_provider(name: str, engine: PolicyEngine):  # type: ignore[no-untyped-def]
    @plugin(name=name, version="1.0.0", provides={"policy": "1.0.0"})
    async def provide(ctx: PluginContext) -> None:
        ctx.capabilities.provide(POLICY, engine)

    return provide


def secret_provider(name: str, values: dict[str, str]):  # type: ignore[no-untyped-def]
    @plugin(name=name, version="1.0.0", provides={"secrets": "1.0.0"})
    async def provide(ctx: PluginContext) -> None:
        ctx.capabilities.provide(SECRETS, StaticSecretProvider(values, name=name))

    return provide


def fetcher():  # type: ignore[no-untyped-def]
    @plugin(name="fetcher", version="1.0.0")
    async def fetcher_plugin(ctx: PluginContext) -> None:
        ctx.tools.register(fetch, policy=ToolPolicy(permissions=("network.fetch",)))

    return fetcher_plugin


async def call_fetch(harness: Harness) -> Any:
    """One tool call through the generation's own policy boundary."""

    generation = harness.current_generation
    assert generation is not None
    environment = harness.run_environment(generation)
    return await environment.executor.execute(
        ToolRequest(name="fetch", args={"url": "https://example.test"}),
        snapshot=harness.tool_snapshot(generation),
        policy=environment.policy,
    )


async def test_two_policy_providers_never_widen_to_allow_all() -> None:
    """Two deny policies must deny; ambiguity must not fall back to the default."""

    harness = Harness()  # permissive default configured; it must never be reached
    harness.install(policy_provider("deny-a", DenyAllPolicy()), entry_id="deny-a")
    harness.install(policy_provider("deny-b", DenyAllPolicy()), entry_id="deny-b")
    harness.install(fetcher(), entry_id="fetcher")
    await harness.start()
    try:
        with pytest.raises(PolicyDenied):
            await call_fetch(harness)
    finally:
        await harness.stop()


async def test_ambiguous_scoped_policies_deny_until_a_scope_preference_selects() -> None:
    harness = Harness()
    harness.composition.child("research")
    harness.install(policy_provider("root-deny", DenyAllPolicy()), entry_id="root-deny")
    harness.install(
        policy_provider("research-grant", GrantPolicy(["network.fetch"])),
        entry_id="research-grant",
        scope="/research",
    )
    harness.install(fetcher(), entry_id="fetcher")
    await harness.start()
    try:
        generation = harness.current_generation
        assert generation is not None

        # Without a preference the two visible providers are ambiguous, and the
        # permissive default must not answer for them.
        with pytest.raises(PolicyDenied):
            await call_fetch(harness)

        # A scope-scoped preference selects the governing policy for that scope.
        harness.prefer_provider("policy", "research-grant", scope="/research")
        scoped = harness.run_environment(generation, scope="/research")
        result = await scoped.executor.execute(
            ToolRequest(name="fetch", args={"url": "https://example.test"}),
            snapshot=harness.tool_snapshot(generation),
            policy=scoped.policy,
        )

        assert result.content == "fetched"
    finally:
        await harness.stop()


async def test_policy_provider_failure_denies_the_tool_call() -> None:
    harness = Harness()
    harness.install(policy_provider("broken", ExplodingPolicy()), entry_id="broken")
    harness.install(fetcher(), entry_id="fetcher")
    await harness.start()
    try:
        with pytest.raises(PolicyDenied) as excinfo:
            await call_fetch(harness)

        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert excinfo.value.context["reason"] == "policy provider failure"
        assert SECRET not in str(excinfo.value)
    finally:
        await harness.stop()


async def test_explicit_preference_selects_the_governing_policy() -> None:
    harness = Harness()
    harness.install(policy_provider("denier", DenyAllPolicy()), entry_id="denier")
    harness.install(policy_provider("granter", GrantPolicy(["network.fetch"])), entry_id="granter")
    harness.install(fetcher(), entry_id="fetcher")
    harness.prefer_provider("policy", "denier")
    await harness.start()
    try:
        with pytest.raises(PolicyDenied):
            await call_fetch(harness)

        harness.prefer_provider("policy", "granter")
        result = await call_fetch(harness)

        assert result.content == "fetched"
    finally:
        await harness.stop()


async def test_system_policy_and_secrets_are_limited_to_visible_scope_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_TOKEN", "sk-env-123456")
    harness = Harness()
    harness.composition.child("left")
    harness.composition.child("right")
    harness.install(
        policy_provider("left-grant", GrantPolicy(["network.fetch"])),
        entry_id="left-grant",
        scope="/left",
    )
    harness.install(
        secret_provider("left-vault", {"API_TOKEN": "sk-left-123456"}),
        entry_id="left-vault",
        scope="/left",
    )
    harness.install(fetcher(), entry_id="fetcher")
    await harness.start()
    try:
        generation = harness.current_generation
        assert generation is not None
        left = harness.run_environment(generation, scope="/left")
        right = harness.run_environment(generation, scope="/right")

        left_result = await left.executor.execute(
            ToolRequest(name="fetch", args={"url": "https://example.test"}),
            snapshot=harness.tool_snapshot(generation, scope="/left"),
            policy=left.policy,
        )
        assert left_result.content == "fetched"

        with pytest.raises(PolicyDenied):
            await right.executor.execute(
                ToolRequest(name="fetch", args={"url": "https://example.test"}),
                snapshot=harness.tool_snapshot(generation, scope="/right"),
                policy=right.policy,
            )

        assert (await left.secrets.get("API_TOKEN")).reveal() == "sk-left-123456"
        with pytest.raises(SecretResolutionError):
            await right.secrets.get("API_TOKEN")
    finally:
        await harness.stop()


async def test_ambiguous_secret_providers_never_widen_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_TOKEN", "sk-env-123456")

    harness = Harness()  # default secrets read the environment; they must not be reached
    harness.install(
        secret_provider("vault-a", {"API_TOKEN": "sk-vault-a-123456"}), entry_id="vault-a"
    )
    harness.install(
        secret_provider("vault-b", {"API_TOKEN": "sk-vault-b-123456"}), entry_id="vault-b"
    )
    await harness.start()
    try:
        generation = harness.current_generation
        assert generation is not None
        environment = harness.run_environment(generation)

        with pytest.raises(SecretResolutionError):
            await environment.secrets.get("API_TOKEN")

        with pytest.raises(SecretResolutionError):
            await environment.secrets.get_optional("API_TOKEN")
    finally:
        await harness.stop()
