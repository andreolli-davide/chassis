from __future__ import annotations

import pytest

from chassis.core.errors import ConfigurationError
from chassis.policy import (
    AllowAllPolicy,
    DenyAllPolicy,
    GrantPolicy,
    Permission,
    PermissionGrant,
    PolicyEngine,
    PolicyRequest,
)


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
