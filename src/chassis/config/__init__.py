"""Declarative configuration: models, loading, and desired-state reconciliation."""

from __future__ import annotations

from chassis.config.loader import PluginCatalog, load_config, parse_config
from chassis.config.models import HarnessConfig, PluginEntryConfig
from chassis.config.reconcile import (
    DesiredStateAction,
    DesiredStateChange,
    InstalledEntry,
    config_fingerprint,
    diff_desired_state,
)

__all__ = [
    "DesiredStateAction",
    "DesiredStateChange",
    "HarnessConfig",
    "InstalledEntry",
    "PluginCatalog",
    "PluginEntryConfig",
    "config_fingerprint",
    "diff_desired_state",
    "load_config",
    "parse_config",
]
