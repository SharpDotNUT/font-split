"""Build configuration and the version fingerprint."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

from .encoder import WOFF2_QUALITY

TOOL_NAME = "font-sharder"
TOOL_PACKAGE = "font-split"
DEFAULT_FEATURES: tuple[str, ...] = (
    "ccmp",
    "liga",
    "locl",
    "kern",
    "mark",
    "mkmk",
    "vert",
    "vrt2",
)
DEFAULT_CHUNK_SIZE = 71680
FINGERPRINT_BYTES = 8
FORMAT = "woff2"

#: Revision of the shard generation pipeline; fingerprint gates artifact reuse.
SHARD_PIPELINE = 3


def tool_version() -> str:
    try:
        return package_version(TOOL_PACKAGE)
    except PackageNotFoundError:  # pragma: no cover - source checkout without install
        return "0.0.0"


def slugify(value: str, fallback: str = "Font") -> str:
    """`Noto Sans SC` -> `NotoSansSC` (contract §1)."""
    slug = "".join(char for char in value if char.isalnum())
    return slug or fallback


@dataclass(frozen=True, slots=True)
class BuildConfig:
    source: Path
    output: Path
    slug: str
    css_family: str
    fallback: Path | None
    chunk_size: int
    features: tuple[str, ...] | None
    deps: str
    include_cjk_ext: bool
    areas: Path | None
    jobs: int
    force: bool
    skip_baseline: bool
    dry_run: bool
    verify: bool
    font_number: int = 0

    def fingerprint(
        self, *, source_sha256: str, fallback_sha256: str | None, areas_version: str
    ) -> str:
        """Short hash over everything that changes the bytes of a build."""
        payload = {
            "source": source_sha256,
            "fallback": fallback_sha256,
            "tool": TOOL_NAME,
            "toolVersion": tool_version(),
            "pipeline": SHARD_PIPELINE,
            "format": FORMAT,
            "woff2Quality": WOFF2_QUALITY,
            "chunkSize": self.chunk_size,
            "layoutFeatures": "all" if self.features is None else list(self.features),
            "deps": self.deps,
            "includeCjkExt": self.include_cjk_ext,
            "areas": areas_version,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.blake2b(
            canonical.encode("utf-8"), digest_size=FINGERPRINT_BYTES
        ).hexdigest()

    def manifest_config(self) -> dict:
        return {
            "chunkSize": self.chunk_size,
            "layoutFeatures": "all" if self.features is None else list(self.features),
            "areas": None,
            "includeCjkExt": self.include_cjk_ext,
            "woff2Quality": WOFF2_QUALITY,
            "deps": self.deps,
            "format": FORMAT,
            "pipeline": SHARD_PIPELINE,
        }
