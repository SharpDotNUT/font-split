"""Contract §7 assertions: the build verifies its own output.

Every check returns a list of human readable problems, so the builder can fail loudly
instead of shipping a directory that silently violates the artifact contract.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import uharfbuzz as hb
from fontTools.ttLib import TTFont, TTLibError

from .encoder import blake2b8_file, to_sfnt
from .layout_deps import Constraint
from .planner import Shard
from .unicode_range import expand_ranges, format_ranges, merge_ranges

SIZE_TOLERANCE = 0.20
SHAPE_SAMPLE_SHARDS = 12
SHAPE_SAMPLE_CHARS = 20
FONT_SMOKE_TABLES = ("head", "maxp", "cmap")

#: Default_Ignorable_Code_Point: HarfBuzz replaces these with the invisible glyph,
#: so a `.notdef` result for them is expected, not a missing character.
_DEFAULT_IGNORABLE = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)


def is_default_ignorable(code: int) -> bool:
    return any(start <= code <= stop for start, stop in _DEFAULT_IGNORABLE)


def _hex(codes: Sequence[int], limit: int = 8) -> str:
    shown = ",".join(f"U+{code:04X}" for code in codes[:limit])
    return shown + ("..." if len(codes) > limit else "")


def check_coverage(
    expected: Sequence[int],
    shards: Sequence[Shard],
    *,
    excluded: Sequence[int] = (),
) -> list[str]:
    """1. every codepoint of the source cmap is covered (minus deliberately excluded ones)."""
    covered = {code for shard in shards for code in shard.codes}
    want = set(expected) - set(excluded)
    problems = []
    if missing := sorted(want - covered):
        problems.append(
            f"coverage: {len(missing)} codepoint(s) missing, e.g. {_hex(missing)}"
        )
    if extra := sorted(covered - want):
        problems.append(
            f"coverage: {len(extra)} codepoint(s) not in the source cmap, e.g. {_hex(extra)}"
        )
    return problems


def check_mutex(shards: Sequence[Shard]) -> list[str]:
    """2. no codepoint appears in two shards, and none repeats inside one shard."""
    seen: set[int] = set()
    duplicates: set[int] = set()
    for shard in shards:
        for code in shard.codes:
            if code in seen:
                duplicates.add(code)
            seen.add(code)
    if duplicates:
        return [
            (
                f"mutex: {len(duplicates)} codepoint(s) appear more than once, "
                f"e.g. {_hex(sorted(duplicates))}"
            ),
        ]
    return []


def check_ranges(shards: Sequence[Shard], directory: Path | None = None) -> list[str]:
    """3. `unicode-range` round-trips, and the encoded font's cmap matches it exactly."""
    problems: list[str] = []
    for shard in shards:
        spec = format_ranges(merge_ranges(shard.codes))
        decoded = expand_ranges(spec)
        if sorted(decoded) != sorted(set(shard.codes)):
            problems.append(
                f"{shard.file or shard.area}: unicode-range does not round-trip"
            )
            continue
        if directory is None or not shard.file:
            continue
        path = directory / shard.file
        try:
            font = TTFont(path, lazy=True, recalcTimestamp=False)
        except (TTLibError, OSError) as exc:
            problems.append(f"{shard.file}: cannot be read back ({exc})")
            continue
        cmap = font.getBestCmap() or {}
        if set(cmap) != set(shard.codes):
            problems.append(
                f"{shard.file}: cmap has {len(cmap)} entries, expected {len(shard.codes)}"
            )
    return problems


def check_constraints(
    constraints: Sequence[Constraint], shards: Sequence[Shard]
) -> list[str]:
    """4. every GSUB/GPOS constraint set ends up in a single shard."""
    owner = {code: shard.index for shard in shards for code in shard.codes}
    violations: list[str] = []
    for constraint in constraints:
        holders = {owner[code] for code in constraint.chars if code in owner}
        if len(holders) > 1:
            violations.append(
                f"{constraint.source}: split across shards {sorted(holders)}"
            )
    if violations:
        return [
            f"constraints: {len(violations)} rule(s) not co-located, e.g. "
            + "; ".join(violations[:3])
        ]
    return []


def check_artifacts(payload: dict, directory: Path) -> list[str]:
    """5. bytes and content hashes in the manifest match the files on disk."""
    problems: list[str] = []
    for entry in payload.get("shards", []):
        path = directory / entry["file"]
        if not path.is_file():
            problems.append(f"{entry['file']}: missing")
            continue
        if path.stat().st_size != entry["bytes"]:
            problems.append(
                f"{entry['file']}: {path.stat().st_size} bytes on disk, {entry['bytes']} in manifest"
            )
        digest = _digest(path)
        if digest != entry["blake2b"]:
            problems.append(
                f"{entry['file']}: blake2b {digest} on disk, {entry['blake2b']} in manifest"
            )
    return problems


def check_fonts(
    shards: Sequence[Shard],
    directory: Path,
    *,
    source_font: bytes | None = None,
) -> list[str]:
    """6. fonts are readable, carry their required tables, and lose no characters."""
    problems: list[str] = []
    step = max(1, len(shards) // SHAPE_SAMPLE_SHARDS)
    for shard in shards[::step]:
        path = directory / shard.file
        try:
            font = TTFont(path, lazy=True, recalcTimestamp=False)
        except (TTLibError, OSError) as exc:
            problems.append(f"{shard.file}: unreadable ({exc})")
            continue
        for tag in FONT_SMOKE_TABLES:
            if tag not in font:
                problems.append(f"{shard.file}: missing '{tag}' table")
        if font.get("maxp") is not None and font["maxp"].numGlyphs <= 0:
            problems.append(f"{shard.file}: numGlyphs is {font['maxp'].numGlyphs}")
        problems.extend(_shape_missing(path, shard))

    if source_font is not None and shards:
        primary = [shard for shard in shards if shard.origin == "primary"]
        problems.extend(_shaping_fidelity(primary, directory, source_font))

    return problems


def _shape_missing(path: Path, shard: Shard) -> list[str]:
    codes = [
        code
        for code in shard.codes[:SHAPE_SAMPLE_CHARS]
        if not is_default_ignorable(code)
    ]
    if not codes:
        return []
    face = hb.Face(to_sfnt(path.read_bytes()))
    font = hb.Font(face)
    missing = [
        code
        for code in codes
        if 0 in [info.codepoint for info in _shape(font, chr(code))]
    ]
    if missing:
        return [
            f"{shard.file}: {len(missing)} character(s) shape to .notdef, e.g. {_hex(missing)}"
        ]
    return []


def _shaping_fidelity(
    shards: Sequence[Shard], directory: Path, source_font: bytes
) -> list[str]:
    """Layout features must still work inside a shard: `liga` and `vert` spot checks.

    Glyph ids are only comparable inside one font, so the feature is exercised by
    turning it *off* and comparing the two results per font: if the source font
    distinguishes the two, the shard has to as well.
    """
    problems: list[str] = []
    source = hb.Font(hb.Face(source_font))

    for text, feature in (("fi", "liga"), ("\u3002", "vert")):
        codes = [ord(char) for char in text]
        holder = next((s for s in shards if all(c in s.codes for c in codes)), None)
        if holder is None:
            continue
        source_on = _codepoints(_shape(source, text, {feature: True}))
        source_off = _codepoints(_shape(source, text, {feature: False}))
        if source_on == source_off:
            continue  # the source font does not use this feature here: nothing to prove

        shard_font = hb.Font(hb.Face(to_sfnt((directory / holder.file).read_bytes())))
        shard_on = _codepoints(_shape(shard_font, text, {feature: True}))
        shard_off = _codepoints(_shape(shard_font, text, {feature: False}))
        if 0 in shard_on:
            problems.append(
                f"{holder.file}: '{feature}' shaping on {text!r} produced .notdef"
            )
            continue
        if len(shard_on) != len(source_on):
            problems.append(
                f"{holder.file}: '{feature}' on {text!r} gives {len(shard_on)} glyph(s), "
                f"source font gives {len(source_on)}"
            )
        elif shard_on == shard_off:
            problems.append(
                f"{holder.file}: '{feature}' has no effect on {text!r} (feature lost)"
            )
    return problems


def _codepoints(infos: Sequence) -> list[int]:
    return [info.codepoint for info in infos]


def _shape(font: hb.Font, text: str, features: dict[str, bool] | None = None) -> list:
    buffer = hb.Buffer()
    buffer.add_str(text)
    buffer.guess_segment_properties()
    hb.shape(font, buffer, features or {})
    return buffer.glyph_infos


def check_css(css_text: str, shards: Sequence[Shard], directory: Path) -> list[str]:
    """7. one `@font-face` per shard, each referencing an existing file exactly once."""
    problems: list[str] = []
    blocks = css_text.count("@font-face")
    if blocks != len(shards):
        problems.append(f"css: {blocks} @font-face blocks for {len(shards)} shards")
    referenced = [
        line.split('url("./', 1)[1].split('")', 1)[0]
        for line in css_text.splitlines()
        if 'url("./' in line
    ]
    if len(referenced) != len(set(referenced)):
        problems.append("css: duplicate url() references")
    if set(referenced) != {shard.file for shard in shards}:
        problems.append("css: referenced files do not match the shard list")
    for name in referenced:
        if not (directory / name).is_file():
            problems.append(f"css: referenced file {name} does not exist")
    return problems


def check_size_baseline(
    predicted: int, actual: int, tolerance: float = SIZE_TOLERANCE
) -> list[str]:
    """8. real bytes must stay within `tolerance` of the dry-run prediction."""
    if predicted <= 0:
        return []
    deviation = (actual - predicted) / predicted
    if abs(deviation) > tolerance:
        return [
            (
                f"size: built {actual} bytes vs {predicted} predicted "
                f"({deviation:+.1%}, tolerance {tolerance:.0%}) - density calibration is off"
            ),
        ]
    return []


def _digest(path: Path) -> str:
    return blake2b8_file(path)
