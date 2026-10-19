"""HarfBuzz subsetting kernel.

The whole build reuses one `hb.Face` per thread: creating a face per shard would
re-parse the (multi-megabyte) source font hundreds of times.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable

import uharfbuzz as hb

MIN_FEATURE_TAG_LENGTH = 4


def feature_tag(name: str) -> int:
    """Convert an OpenType feature tag such as ``liga`` into an ``hb_tag_t``."""
    try:
        raw = name.encode("ascii")
    except UnicodeEncodeError as exc:  # pragma: no cover - defensive
        raise ValueError(f"feature tag {name!r} must be ASCII") from exc
    if len(raw) != MIN_FEATURE_TAG_LENGTH:
        raise ValueError(f"feature tag {name!r} must be exactly 4 characters")
    return int.from_bytes(raw, "big")


class Subsetter:
    """Turns one source font into per-shard OpenType subsets."""

    def __init__(self, font_bytes: bytes, features: tuple[str, ...] | None) -> None:
        self._bytes = font_bytes
        self._features = features
        self._tags = (
            None if features is None else {feature_tag(name) for name in features}
        )
        self._local = threading.local()

    @property
    def features(self) -> tuple[str, ...] | None:
        return self._features

    def subset(self, codes: Iterable[int]) -> bytes:
        """Subset the source font down to `codes` (plus layout dependencies)."""
        inp = hb.SubsetInput()
        # keep every table we know how to carry over (cn-font-split's clear_drop_table)
        inp.drop_table_tag_set.clear()
        inp.layout_script_tag_set.clear()
        inp.layout_script_tag_set.invert()  # every script
        inp.layout_feature_tag_set.clear()
        if self._tags is None:
            inp.layout_feature_tag_set.invert()  # every feature (--features all)
        else:
            inp.layout_feature_tag_set.update(self._tags)
        inp.unicode_set.clear()
        for code in codes:
            inp.unicode_set.add(code)
        inp.flags |= hb.SubsetFlags.NOTDEF_OUTLINE

        blob = hb.subset(self._face(), inp).blob
        return memoryview(blob.data).tobytes()

    def _face(self) -> hb.Face:
        face = getattr(self._local, "face", None)
        if face is None:
            face = hb.Face(self._bytes)
            self._local.face = face
        return face
