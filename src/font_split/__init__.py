"""Pre-shard fonts into Unicode-aware WOFF2 shards with CSS and a manifest.

Entry point: `font-sharder`. Contract: `docs/font-artifact-contract.md`.
"""

from __future__ import annotations

__all__ = ["build", "cli"]


def __getattr__(name: str):  # lazy, so `python -m font_split` stays cheap
    if name == "build":
        from .build import build

        return build
    if name == "cli":
        from . import cli

        return cli
    raise AttributeError(name)
