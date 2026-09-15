"""Plugin lifecycle: manifests, the author API, resolution, and mounting."""

from __future__ import annotations

from chassis.plugins.base import Plugin, PluginContext, plugin
from chassis.plugins.lifecycle import PluginHealth, PluginInstance, PluginState
from chassis.plugins.manifest import PluginManifest
from chassis.plugins.registry import PluginEntry, PluginRegistry
from chassis.plugins.resolver import (
    DependencyResolver,
    PluginCandidate,
    PluginPlan,
    ProviderOption,
    RequirementResolution,
    ResolutionPlan,
)

__all__ = [
    "DependencyResolver",
    "Plugin",
    "PluginCandidate",
    "PluginContext",
    "PluginEntry",
    "PluginHealth",
    "PluginInstance",
    "PluginManifest",
    "PluginPlan",
    "PluginRegistry",
    "PluginState",
    "ProviderOption",
    "RequirementResolution",
    "ResolutionPlan",
    "plugin",
]
