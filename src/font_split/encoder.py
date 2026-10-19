"""WOFF2 encoding and content hashing.

fontTools' WOFF2 writer applies the glyf/loca transform and compresses with brotli
`MODE_FONT` at the library default quality 11, so no C++ woff2 build is needed.
The output is deterministic, which is what makes the content hash in the file name
meaningful.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterable

from fontTools.ttLib import TTFont

DIGEST_SIZE = 8
WOFF2_QUALITY = 11


def encode_woff2(ttf_bytes: bytes) -> bytes:
    # recalcTimestamp must stay off: fontTools would otherwise stamp head.modified
    # with the current time, making the bytes (and therefore the content hash in the
    # file name) different on every run.
    font = TTFont(io.BytesIO(ttf_bytes), recalcTimestamp=False)
    font.flavor = "woff2"
    buffer = io.BytesIO()
    font.save(buffer)
    return buffer.getvalue()


def to_sfnt(data: bytes) -> bytes:
    """Return plain sfnt bytes: HarfBuzz cannot read WOFF/WOFF2 containers."""
    if data[:4] not in (b"wOF2", b"wOFF"):
        return data
    font = TTFont(io.BytesIO(data), recalcTimestamp=False)
    flavor, font.flavor = font.flavor, None
    buffer = io.BytesIO()
    font.save(buffer)
    font.flavor = flavor
    return buffer.getvalue()


def prune_cmap(ttf_bytes: bytes, codes: Iterable[int]) -> bytes:
    """Reduce a subset's cmap to exactly `codes`.

    HarfBuzz keeps every mapping whose glyph survived the closure, so a shard can end
    up declaring codepoints that its `unicode-range` does not cover: a mirror pair such
    as U+27E6/U+27E7 pulls each other's glyph in, and both mappings stay. Those extra
    entries are unreachable from CSS (the browser only applies a shard to characters in
    its `unicode-range`) and only make the artifact disagree with the contract, so they
    are dropped here.

    Does nothing when the cmap is already exact, or when it is *missing* requested
    codepoints - that is a real defect and is left for the contract checks to report.
    """
    wanted = sorted(set(codes))
    font = TTFont(io.BytesIO(ttf_bytes), recalcTimestamp=False)
    current = font.getBestCmap() or {}
    if len(current) == len(wanted) or any(code not in current for code in wanted):
        return ttf_bytes

    mapping = {code: current[code] for code in wanted}
    bmp = {code: name for code, name in mapping.items() if code <= 0xFFFF}
    tables = []
    if bmp:
        tables.append(_cmap_subtable(4, 3, 1, bmp))
    if len(mapping) != len(bmp):
        tables.append(_cmap_subtable(12, 3, 10, mapping))

    cmap = font["cmap"]
    cmap.tableVersion = 0
    cmap.tables = tables
    buffer = io.BytesIO()
    font.save(buffer)
    return buffer.getvalue()


def _cmap_subtable(format: int, platform: int, encoding: int, mapping: dict[int, str]):
    from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

    table = CmapSubtable.newSubtable(format)
    table.platformID = platform
    table.platEncID = encoding
    table.language = 0
    table.cmap = mapping
    return table


def blake2b8(data: bytes) -> str:
    """Short content hash used in shard file names (contract §1)."""
    return hashlib.blake2b(data, digest_size=DIGEST_SIZE).hexdigest()


def blake2b8_file(path) -> str:
    """Stream a file through blake2b to validate an already-built shard."""
    digest = hashlib.blake2b(digest_size=DIGEST_SIZE)
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def glyph_count(ttf_bytes: bytes) -> int:
    return int(
        TTFont(io.BytesIO(ttf_bytes), lazy=True, recalcTimestamp=False)[
            "maxp"
        ].numGlyphs
    )
