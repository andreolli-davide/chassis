"""Capability contracts.

A capability is a *logical service contract*, not a plugin. Consumers declare
which contracts they need; providers register an implementation under a contract.
Nothing in this module knows about concrete plugins.

Two versioning concepts exist and must not be conflated:

``CapabilityKey.api_version``
    The *contract generation* -- the major version of the contract a provider
    implements (``model@1``). Two providers on different generations are
    different contracts and cannot satisfy each other's requirements.

``CapabilityRegistration.version``
    The concrete implementation version a provider advertises (``1.4.0``). A
    requirement's ``SpecifierSet`` is matched against this value.

Requirements are parsed with :mod:`packaging`; Chassis never implements its own
version algebra. A requirement's contract generation is derived from the lowest
lower bound of its specifier (``>=2,<3`` implies ``@2``); a requirement with no
lower bound (for example the empty specifier) is generation-agnostic and matches
any provider advertising a satisfying implementation version.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from chassis.core.errors import ConfigurationError

__all__ = [
    "ARTIFACTS",
    "DATABASE",
    "MEMORY",
    "MODEL",
    "POLICY",
    "SANDBOX",
    "SCHEDULER",
    "SECRETS",
    "TOOLS",
    "CapabilityKey",
    "CapabilityRequirement",
]

_LOWER_BOUND_OPERATORS = (">=", "~=", "===", "==", ">")


@dataclass(frozen=True, slots=True, order=True)
class CapabilityKey:
    """Identity of a capability contract: ``name`` plus contract generation.

    Args:
        name: Capability name, for example ``"model"``.
        api_version: Contract generation, normally the major version of the
            implementation version. The empty string marks a
            generation-agnostic requirement and MUST NOT be used for providers.
    """

    name: str
    api_version: str = "1"

    def __post_init__(self) -> None:
        if not self.name or self.name != self.name.strip():
            raise ConfigurationError(
                "capability name must be a non-empty, trimmed string", capability=self.name
            )
        if self.api_version and not self.api_version.isdigit():
            raise ConfigurationError(
                "capability api_version must be a numeric contract generation",
                capability=self.name,
                api_version=self.api_version,
            )

    @classmethod
    def from_version(cls, name: str, version: str | Version) -> CapabilityKey:
        """Derive a contract key from a provided implementation version.

        ``provides={"database": "2.1.0"}`` registers ``database@2``.
        """

        parsed = parse_version(version, capability=name)
        return cls(name=name, api_version=str(parsed.major))

    def __str__(self) -> str:
        return f"{self.name}@{self.api_version}" if self.api_version else self.name


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    """A consumer's requirement on a capability contract."""

    key: CapabilityKey
    specifier: SpecifierSet = field(default_factory=lambda: SpecifierSet(""))
    optional: bool = False

    @classmethod
    def parse(
        cls,
        name: str,
        requirement: str = "",
        *,
        optional: bool = False,
        api_version: str | None = None,
    ) -> CapabilityRequirement:
        """Parse a manifest-style requirement such as ``">=1,<2"``.

        A bare version (``"1.2.0"``) is normalized to an exact match so that
        plugin manifests stay readable.
        """

        specifier = parse_specifier(requirement, capability=name)
        if api_version is None:
            api_version = specifier_major(specifier) or ""
        return cls(
            key=CapabilityKey(name=name, api_version=api_version),
            specifier=specifier,
            optional=optional,
        )

    @property
    def name(self) -> str:
        return self.key.name

    @property
    def is_generation_agnostic(self) -> bool:
        return not self.key.api_version

    def accepts(self, key: CapabilityKey, version: Version) -> bool:
        """Whether a provider offering ``key`` at ``version`` satisfies this requirement."""

        if key.name != self.key.name:
            return False
        if self.key.api_version and key.api_version != self.key.api_version:
            return False
        return version in self.specifier

    def __str__(self) -> str:
        rendered = str(self.specifier) or "*"
        return (
            f"{self.name} {rendered}" if not self.optional else f"{self.name} {rendered} (optional)"
        )


def parse_version(value: str | Version, *, capability: str) -> Version:
    """Parse a version string using :mod:`packaging`."""

    if isinstance(value, Version):
        return value
    try:
        return Version(value)
    except InvalidVersion as error:
        raise ConfigurationError(
            "capability version is not PEP 440 compliant",
            capability=capability,
            version=value,
        ) from error


def parse_specifier(value: str | SpecifierSet, *, capability: str) -> SpecifierSet:
    """Parse a requirement string into a :class:`~packaging.specifiers.SpecifierSet`."""

    if isinstance(value, SpecifierSet):
        return value
    text = value.strip()
    if text and text[0] not in "<>=!~":
        text = f"=={text}"
    try:
        return SpecifierSet(text)
    except InvalidSpecifier as error:
        raise ConfigurationError(
            "capability requirement is not a valid PEP 440 specifier set",
            capability=capability,
            requirement=value,
        ) from error


def specifier_major(specifier: SpecifierSet) -> str | None:
    """Return the contract generation the specifier proves, if it proves one.

    A range pins a single API major only when its accepted versions cannot cross
    a major boundary: an exact or wildcard version, a compatible release, or
    bounds that agree on the major (``>=1.4,<2``). Open and multi-major ranges
    such as ``>1.9,<3`` yield ``None`` — pinning them to the lower bound would
    silently reject providers the range accepts. Exclusions never widen a range
    and are ignored.
    """

    pinned: list[Version] = []
    lower: list[Version] = []
    upper: list[tuple[str, Version]] = []
    for spec in specifier:
        if spec.operator == "!=":
            continue
        raw = spec.version[:-2] if spec.version.endswith(".*") else spec.version
        try:
            version = Version(raw)
        except InvalidVersion:
            continue
        if spec.operator in ("==", "==="):
            pinned.append(version)
        elif spec.operator == "~=":
            # A compatible release never crosses a major boundary.
            pinned.append(version)
        elif spec.operator in _LOWER_BOUND_OPERATORS:
            lower.append(version)
        elif spec.operator in ("<", "<="):
            upper.append((spec.operator, version))
    if pinned:
        return str(min(pinned).major)
    if not lower or not upper:
        return None
    floor = max(lower)
    operator, ceiling = min(upper, key=lambda item: item[1])
    if operator == "<" and (ceiling.minor, ceiling.micro) == (0, 0):
        ceiling_major = ceiling.major - 1
    else:
        ceiling_major = ceiling.major
    return str(floor.major) if floor.major == ceiling_major else None


#: Built-in capability contracts used by Chassis and its examples.
MODEL = CapabilityKey("model", api_version="1")
TOOLS = CapabilityKey("tools", api_version="1")
MEMORY = CapabilityKey("memory", api_version="1")
DATABASE = CapabilityKey("database", api_version="1")
SANDBOX = CapabilityKey("sandbox", api_version="1")
ARTIFACTS = CapabilityKey("artifacts", api_version="1")
SCHEDULER = CapabilityKey("scheduler", api_version="1")
POLICY = CapabilityKey("policy", api_version="1")
SECRETS = CapabilityKey("secrets", api_version="1")
