"""Exception hierarchy for the sharder."""

from __future__ import annotations


class FontSplitError(Exception):
    """Base class for all sharder failures."""


class ConfigError(FontSplitError):
    """A configuration value or vendored area table is invalid."""
