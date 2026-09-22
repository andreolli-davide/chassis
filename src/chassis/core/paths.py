"""Canonical composition-scope paths.

One definition shared by the composition tree, the harness, and `AgentSpec`, so
every API that accepts a scope path accepts the same canonical form.
"""

from __future__ import annotations

from chassis.core.errors import ConfigurationError

__all__ = ["ROOT_PATH", "canonical_scope_path"]

#: Path of the implicit root scope of every composition.
ROOT_PATH = "/"


def canonical_scope_path(path: str) -> str:
    """Validate and return a canonical composition-scope path.

    Canonical means: absolute, ``/``-separated, no empty or untrimmed segments,
    no ``.``/``..`` segments, and no trailing slash except for the root ``"/"``.

    Raises:
        ConfigurationError: the path is not canonical.
    """

    if not isinstance(path, str) or not path.startswith("/"):
        raise ConfigurationError("scope path must be an absolute path", scope=str(path))
    if path == ROOT_PATH:
        return path
    if path.endswith("/"):
        raise ConfigurationError("scope path must not end with '/'", scope=path)
    for segment in path[1:].split("/"):
        if not segment or segment != segment.strip():
            raise ConfigurationError(
                "scope path segments must be non-empty and trimmed", scope=path
            )
        if segment in (".", ".."):
            raise ConfigurationError("scope path segment is reserved", scope=path)
    return path
