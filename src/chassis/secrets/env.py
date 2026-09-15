"""Secret providers usable without external infrastructure."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

from chassis.core.errors import SecretResolutionError
from chassis.secrets.base import SecretValue

__all__ = ["EnvSecretProvider", "StaticSecretProvider"]

_NAME_SEPARATORS = str.maketrans({".": "_", "-": "_", "/": "_"})


def _environment_name(name: str, prefix: str) -> str:
    return f"{prefix}{name.translate(_NAME_SEPARATORS).upper()}"


class EnvSecretProvider:
    """Resolves secrets from environment variables.

    A dotted name such as ``openai.api_key`` maps to ``OPENAI_API_KEY``. Names may
    be remapped explicitly with ``aliases``.

    Args:
        prefix: Optional environment variable prefix, for example ``"CHASSIS_"``.
        aliases: Secret name to environment variable name.
        environ: Environment mapping; defaults to :data:`os.environ`.
    """

    def __init__(
        self,
        *,
        prefix: str = "",
        aliases: Mapping[str, str] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._prefix = prefix
        self._aliases = dict(aliases or {})
        self._environ = environ

    @property
    def source(self) -> str:
        return "environment"

    def environment_name(self, name: str) -> str:
        """The environment variable consulted for ``name``."""

        alias = self._aliases.get(name)
        if alias is not None:
            return alias
        return _environment_name(name, self._prefix)

    def _lookup(self, name: str) -> str | None:
        environ = os.environ if self._environ is None else self._environ
        return environ.get(self.environment_name(name))

    async def get(self, name: str) -> SecretValue:
        value = self._lookup(name)
        if value is None or value == "":
            raise SecretResolutionError(
                f"secret {name!r} is not set in the environment",
                secret=name,
                provider="environment",
                environment_variable=self.environment_name(name),
            )
        return SecretValue.of(name, value)

    async def get_optional(self, name: str) -> SecretValue | None:
        value = self._lookup(name)
        if value is None or value == "":
            return None
        return SecretValue.of(name, value)


class StaticSecretProvider:
    """In-memory secret provider for tests and embedded configuration.

    Args:
        values: Secret name to value.
        name: Provider label used in diagnostics and errors.
    """

    def __init__(self, values: Mapping[str, str] | None = None, *, name: str = "static") -> None:
        self._values = dict(values or {})
        self._name = name

    @property
    def source(self) -> str:
        return self._name

    def names(self) -> tuple[str, ...]:
        """Secret names held by this provider. Values are never exposed."""

        return tuple(sorted(self._values))

    def add(self, name: str, value: str) -> None:
        self._values[name] = value

    async def get(self, name: str) -> SecretValue:
        value = self._values.get(name)
        if value is None:
            raise SecretResolutionError(
                f"secret {name!r} is not available from {self._name}",
                secret=name,
                provider=self._name,
            )
        return SecretValue.of(name, value)

    async def get_optional(self, name: str) -> SecretValue | None:
        value = self._values.get(name)
        if value is None:
            return None
        return SecretValue.of(name, value)

    def to_dict(self) -> dict[str, Iterable[str]]:
        return {"provider": [self._name], "names": self.names()}
