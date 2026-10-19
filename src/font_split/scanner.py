"""cmap scanning and area bucketing."""

from __future__ import annotations

from dataclasses import dataclass

from fontTools.ttLib import TTFont

from .areas import CJK_EXT_AREA, MISC_AREA, AreaTable, is_cjk_ext


@dataclass(frozen=True, slots=True)
class Bucket:
    """An ordered group of codepoints that will be split into one or more shards."""

    area: str
    tag: str
    order: str
    codes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Bucketing:
    buckets: tuple[Bucket, ...]
    total: int
    assigned: int
    dropped: tuple[int, ...]


def scan_codepoints(font: TTFont) -> list[int]:
    """Every codepoint the font actually supports (HarfBuzz `collect_unicodes` equivalent)."""
    cmap = font.getBestCmap()
    if not cmap:
        raise ValueError("font has no usable Unicode cmap")
    return sorted(cmap)


def bucket_codepoints(
    codepoints: list[int],
    table: AreaTable,
    *,
    include_cjk_ext: bool = True,
) -> Bucketing:
    """Assign every codepoint to exactly one area, in table order (contract §5.2).

    Areas claim characters in table order and keep their own internal order, so a
    frequency ordered area keeps frequency order. Whatever is left is split into the
    two fallback buckets: `cjk-ext` (CJK ideograph blocks) and `misc` (everything else).
    """
    remaining = set(codepoints)
    buckets: list[Bucket] = []
    for area in table.areas:
        picked = [code for code in area.codepoints if code in remaining]
        if not picked:
            continue
        remaining.difference_update(picked)
        buckets.append(
            Bucket(area=area.name, tag=area.tag, order=area.order, codes=tuple(picked))
        )

    dropped: tuple[int, ...] = ()
    if include_cjk_ext:
        ext = sorted(code for code in remaining if is_cjk_ext(code))
        if ext:
            buckets.append(
                Bucket(
                    area=CJK_EXT_AREA,
                    tag="cjk-ext",
                    order="codepoint",
                    codes=tuple(ext),
                )
            )
            remaining.difference_update(ext)
    else:
        dropped = tuple(sorted(code for code in remaining if is_cjk_ext(code)))
        remaining.difference_update(dropped)

    rest = sorted(remaining)
    if rest:
        buckets.append(
            Bucket(area=MISC_AREA, tag="misc", order="codepoint", codes=tuple(rest))
        )

    assigned = sum(len(bucket.codes) for bucket in buckets)
    return Bucketing(
        buckets=tuple(buckets),
        total=len(codepoints),
        assigned=assigned,
        dropped=dropped,
    )
