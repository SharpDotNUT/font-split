"""Unicode range merging and formatting (the `unicode-range` contract)."""

from __future__ import annotations

import re
from collections.abc import Iterable

_RANGE_RE = re.compile(r"^U\+([0-9A-Fa-f]{1,6})(?:-([0-9A-Fa-f]{1,6}))?$")


def merge_ranges(codes: Iterable[int]) -> list[tuple[int, int]]:
    """Merge codepoints into sorted, non-adjacent ``(start, end)`` inclusive ranges."""
    ordered = sorted(set(codes))
    ranges: list[tuple[int, int]] = []
    for code in ordered:
        if ranges and code == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], code)
        else:
            ranges.append((code, code))
    return ranges


def format_ranges(ranges: Iterable[tuple[int, int]]) -> str:
    """Render ranges as the compact `unicode-range` form used by CSS and the manifest."""
    parts = []
    for start, end in ranges:
        parts.append(f"U+{start:04X}" if start == end else f"U+{start:04X}-{end:04X}")
    return ",".join(parts)


def format_codes(codes: Iterable[int]) -> str:
    return format_ranges(merge_ranges(codes))


def expand_ranges(spec: str) -> list[int]:
    """Inverse of :func:`format_ranges`; raises on malformed input."""
    out: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = _RANGE_RE.match(chunk)
        if match is None:
            raise ValueError(
                f"malformed unicode-range segment: {chunk!r} (in {spec!r})"
            )
        start = int(match.group(1), 16)
        end = int(match.group(2), 16) if match.group(2) else start
        if end < start:
            raise ValueError(f"inverted unicode-range segment: {chunk!r}")
        out.extend(range(start, end + 1))
    return out
