from __future__ import annotations

import pytest

from chassis.core.errors import SecretResolutionError
from chassis.secrets import (
    REDACTED,
    EnvSecretProvider,
    SecretProvider,
    SecretRedactor,
    SecretValue,
    StaticSecretProvider,
    redact,
)

SECRET = "sk-live-0123456789abcdef"


def test_secret_value_refuses_to_render_itself() -> None:
    secret = SecretValue.of("openai.api_key", SECRET)

    assert secret.reveal() == SECRET
    assert repr(secret) == "SecretValue(name='openai.api_key', value=<redacted>)"
    assert str(secret) == "<redacted>"
    assert secret.to_dict() == {"name": "openai.api_key", "value": "<redacted>"}
    assert SECRET not in f"{secret!r} {secret!s} {secret.to_dict()}"


async def test_static_provider_resolves_and_reports_missing() -> None:
    provider = StaticSecretProvider({"openai.api_key": SECRET}, name="test")

    assert (await provider.get("openai.api_key")).reveal() == SECRET
    assert await provider.get_optional("absent") is None
    assert provider.names() == ("openai.api_key",)

    with pytest.raises(SecretResolutionError) as excinfo:
        await provider.get("absent")

    assert excinfo.value.context["secret"] == "absent"
    assert SECRET not in str(excinfo.value)


async def test_env_provider_translates_dotted_names() -> None:
    provider = EnvSecretProvider(environ={"OPENAI_API_KEY": SECRET, "MY_TOKEN": SECRET})

    assert provider.environment_name("openai.api_key") == "OPENAI_API_KEY"
    assert (await provider.get("openai.api_key")).reveal() == SECRET
    assert (await provider.get("my-token")).reveal() == SECRET


async def test_env_provider_supports_prefix_and_aliases() -> None:
    provider = EnvSecretProvider(
        prefix="CHASSIS_",
        aliases={"openai.api_key": "CUSTOM_KEY"},
        environ={"CHASSIS_DATABASE_URL": "postgres://x", "CUSTOM_KEY": SECRET},
    )

    assert provider.environment_name("database.url") == "CHASSIS_DATABASE_URL"
    assert provider.environment_name("openai.api_key") == "CUSTOM_KEY"
    assert (await provider.get("database.url")).reveal() == "postgres://x"


async def test_env_provider_reports_missing_without_a_value() -> None:
    provider = EnvSecretProvider(environ={})

    with pytest.raises(SecretResolutionError) as excinfo:
        await provider.get("openai.api_key")

    assert excinfo.value.context["environment_variable"] == "OPENAI_API_KEY"
    assert await provider.get_optional("openai.api_key") is None


async def test_providers_satisfy_the_secret_provider_protocol() -> None:
    assert isinstance(StaticSecretProvider(), SecretProvider)
    assert isinstance(EnvSecretProvider(environ={}), SecretProvider)


def test_redactor_scrubs_text_and_structured_payloads() -> None:
    redactor = SecretRedactor([SECRET, "another-secret-value"])

    assert redactor.redact(f"token={SECRET}") == f"token={REDACTED}"
    assert redactor.redact_value(
        {"api_key": SECRET, "nested": [f"x {SECRET}", {"deep": "another-secret-value"}]}
    ) == {
        "api_key": REDACTED,
        "nested": [f"x {REDACTED}", {"deep": REDACTED}],
    }


def test_redactor_protects_short_secret_values() -> None:
    redactor = SecretRedactor()

    assert redactor.add("ab") is True
    assert redactor.add("") is False  # no material to protect
    assert redactor.add("long-enough") is True
    assert len(redactor) == 2
    assert redactor.redact("zab") == f"z{REDACTED}"


def test_redactor_replaces_longer_values_first() -> None:
    redactor = SecretRedactor(["short-secret", "short-secret-with-suffix"])

    assert redactor.redact("short-secret-with-suffix") == REDACTED


def test_redaction_helper_leaves_unrelated_values_untouched() -> None:
    payload = {"model": "gpt", "key": SECRET, "count": 3}

    assert redact(payload, [SECRET]) == {"model": "gpt", "key": REDACTED, "count": 3}


def test_redactor_tracks_a_secret_value_object() -> None:
    redactor = SecretRedactor()

    assert redactor.add_secret(SecretValue.of("k", SECRET)) is True
    assert redactor.redact(f"key is {SECRET}") == f"key is {REDACTED}"
