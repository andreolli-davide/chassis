"""Optional integration boundaries.

The Chassis kernel -- scopes, plugins, capabilities, generations, budgets,
diagnostics -- depends only on ``pydantic``, ``PyYAML``, and ``packaging``.
LangGraph, ``langchain-core``, and LangSmith are *extras*: importing ``chassis``
must work without them, and using an integration whose extra is not installed must
fail with an actionable message instead of a bare ``ImportError``.

An integration module calls :func:`require_extra` before importing its optional
dependencies. It is a no-op when the extra is installed, so the check never costs
anything at runtime beyond the first import.
"""

from __future__ import annotations

from importlib.util import find_spec

__all__ = ["EXTRA_LANGGRAPH", "EXTRA_LANGSMITH", "MissingExtraError", "require_extra"]

#: Extra that installs the LangGraph adapter and its ``langchain-core`` tools.
EXTRA_LANGGRAPH = "langgraph"
#: Extra that installs the LangSmith telemetry backend.
EXTRA_LANGSMITH = "langsmith"


class MissingExtraError(ImportError):
    """Raised when an optional integration is used without its extra installed.

    Subclasses :class:`ImportError`, so ``except ImportError`` still catches it,
    and :class:`~chassis.core.errors.ChassisError` is deliberately not a base:
    this is an installation problem, not a runtime failure of the composition.
    """

    def __init__(self, extra: str, modules: tuple[str, ...], purpose: str) -> None:
        self.extra = extra
        self.modules = modules
        self.purpose = purpose
        listed = ", ".join(modules)
        verb = "is" if len(modules) == 1 else "are"
        super().__init__(
            f"Chassis requires the {extra!r} extra for {purpose}, but {listed} {verb} not "
            f"installed. Install it with: pip install 'chassis-harness[{extra}]'"
        )


def _absent(modules: tuple[str, ...]) -> tuple[str, ...]:
    missing: list[str] = []
    for module in modules:
        try:
            found = find_spec(module)
        except (ImportError, ValueError):
            found = None
        if found is None:
            missing.append(module)
    return tuple(missing)


def require_extra(extra: str, *modules: str, purpose: str) -> None:
    """Raise :class:`MissingExtraError` unless every ``module`` is importable.

    Args:
        extra: Name of the distribution extra that provides the modules.
        modules: Import names of the optional dependencies (top-level names, so
            checking them never imports the package).
        purpose: Human-readable description of what needs the extra, used in the
            error message.
    """

    missing = _absent(tuple(modules))
    if missing:
        raise MissingExtraError(extra, missing, purpose)
