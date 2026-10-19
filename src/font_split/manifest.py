"""`manifest.json`: the machine readable mirror of a build, and the only source of truth.

The CSS is generated from this structure, and CI (or a future uploader) uses
`shards[].file` / `bytes` / `blake2b` as its verification checklist.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from .planner import Shard
from .unicode_range import format_ranges, merge_ranges

SCHEMA = 2
PREVIEW_CHARS = 10


def build_manifest(
    *,
    font_family: str,
    slug: str,
    fingerprint: str,
    source: dict,
    fallback: dict | None,
    tool: dict,
    config: dict,
    summary: dict,
    shards: Sequence[dict],
) -> dict:
    return {
        "schema": SCHEMA,
        "fontFamily": font_family,
        "slug": slug,
        "fingerprint": fingerprint,
        "build": {
            "source": source,
            "fallback": fallback,
            "tool": tool,
            "config": config,
            "summary": summary,
        },
        "shards": list(shards),
    }


def shard_entry(shard: Shard) -> dict:
    """One `shards[]` record; `unicodeRanges` is generated from the real character set.

    A shard that has not been encoded yet (dry run) reports its prediction and is
    flagged, so a manifest never mixes measured and estimated byte counts silently.
    """
    ranges = merge_ranges(shard.codes)
    predicted = not shard.size
    record = {
        "file": shard.file,
        "order": shard.index,
        "tag": shard.tag,
        "area": shard.area,
        "origin": shard.origin,
        "chars": shard.chars,
        "bytes": shard.size or shard.predicted_bytes,
        "glyphs": shard.glyphs,
        "segments": len(ranges),
        "unicodeRanges": format_ranges(ranges),
        "blake2b": shard.digest,
        "unmappedDeps": shard.unmapped_deps,
        "preview": {
            "first": "".join(chr(code) for code in shard.codes[:PREVIEW_CHARS]),
            "last": f"U+{shard.codes[-1]:04X}" if shard.codes else "",
        },
    }
    if predicted:
        record["predicted"] = True
    return record


def write_manifest(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def load_manifest(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
