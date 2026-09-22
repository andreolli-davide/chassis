"""Composition-provided secret providers must teach the shared redactor (R003).

The default secret provider is wrapped in ``RedactingSecretProvider``; a
provider registered through the composition for the ``secrets`` capability must
be wrapped the same way at every handout seam, or its values reach telemetry,
replay records, and error strings unredacted. Every assertion is an
absence-of-secret assertion.
"""

from __future__ import annotations

from chassis import Harness, PluginContext, plugin
from chassis.capabilities import SECRETS
from chassis.secrets import (
    REDACTED,
    RedactingSecretProvider,
    SecretProvider,
    SecretRedactor,
    StaticSecretProvider,
)
from chassis.telemetry import RecordingTelemetry

SECRET = "sk-live-composition-provided-0123456789"


def vault(provider: SecretProvider | None = None):  # type: ignore[no-untyped-def]
    """A plugin that registers a secrets provider for its composition."""

    selected = (
        provider
        if provider is not None
        else StaticSecretProvider({"db-password": SECRET}, name="vault")
    )

    @plugin(name="vault", version="1.0.0", provides={"secrets": "1.0.0"})
    async def provide(ctx: PluginContext) -> None:
        ctx.capabilities.provide(SECRETS, selected)

    return provide


def consumer(read_secret):  # type: ignore[no-untyped-def]
    """A plugin whose setup reads a secret through ``read_secret``."""

    @plugin(name="consumer", version="1.0.0", requires={"secrets": ">=1,<2"})
    async def consume(ctx: PluginContext) -> None:
        await read_secret(ctx)

    return consume


async def test_environment_secrets_teach_the_shared_redactor() -> None:
    recorder = RecordingTelemetry()
    harness = Harness(telemetry=recorder)
    harness.install(vault(), entry_id="vault")
    try:
        await harness.start()
        generation = harness.current_generation
        assert generation is not None
        environment = harness.run_environment(generation)

        secret = await environment.secrets.get("db-password")
        assert secret.reveal() == SECRET  # still resolvable

        # The value joins the shared redactor at read time ...
        assert harness.redactor.redact(f"password={SECRET}") == f"password={REDACTED}"
        # ... so anything emitted afterwards is free of it.
        harness.telemetry.event("debug", {"note": f"password={SECRET}"})
        assert SECRET not in repr([event.to_dict() for event in recorder.events])
    finally:
        await harness.stop()


async def test_plugin_context_secrets_teach_the_shared_redactor() -> None:
    recorder = RecordingTelemetry()
    harness = Harness(telemetry=recorder)

    async def read(ctx: PluginContext) -> None:
        secret = await ctx.secrets.get("db-password")
        assert secret.reveal() == SECRET

    harness.install(vault(), entry_id="vault")
    harness.install(consumer(read), entry_id="consumer")
    try:
        await harness.start()
        assert harness.redactor.redact(f"password={SECRET}") == f"password={REDACTED}"
        harness.telemetry.event("debug", {"note": f"password={SECRET}"})
        assert SECRET not in repr([event.to_dict() for event in recorder.events])
    finally:
        await harness.stop()


async def test_require_and_get_hand_out_a_redacting_provider_too() -> None:
    recorder = RecordingTelemetry()
    harness = Harness(telemetry=recorder)

    async def read(ctx: PluginContext) -> None:
        for provider in (ctx.require("secrets"), ctx.get("secrets")):
            assert provider is not None
            secret = await provider.get("db-password")
            assert secret.reveal() == SECRET

    harness.install(vault(), entry_id="vault")
    harness.install(consumer(read), entry_id="consumer")
    try:
        await harness.start()
        assert harness.redactor.redact(f"password={SECRET}") == f"password={REDACTED}"
        harness.telemetry.event("debug", {"note": f"password={SECRET}"})
        assert SECRET not in repr([event.to_dict() for event in recorder.events])
    finally:
        await harness.stop()


async def test_a_provider_with_its_own_redactor_also_joins_the_harness_boundary() -> None:
    recorder = RecordingTelemetry()
    foreign_redactor = SecretRedactor()
    provider = RedactingSecretProvider(
        StaticSecretProvider({"db-password": SECRET}, name="foreign-vault"),
        foreign_redactor,
    )
    harness = Harness(telemetry=recorder)

    async def read(ctx: PluginContext) -> None:
        for resolved in (ctx.secrets, ctx.require("secrets"), ctx.get("secrets")):
            assert resolved is not None
            secret = await resolved.get("db-password")
            assert secret.reveal() == SECRET

    harness.install(vault(provider), entry_id="vault")
    harness.install(consumer(read), entry_id="consumer")
    try:
        await harness.start()
        generation = harness.current_generation
        assert generation is not None
        secret = await harness.run_environment(generation).secrets.get("db-password")
        assert secret.reveal() == SECRET

        assert harness.redactor.redact(SECRET) == REDACTED
        assert foreign_redactor.redact(SECRET) == REDACTED
        harness.telemetry.event("debug", {"note": SECRET})
        assert SECRET not in repr([event.to_dict() for event in recorder.events])
    finally:
        await harness.stop()
