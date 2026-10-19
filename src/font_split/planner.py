"""Shard planning: cube-root splitting plus cross-shard dependency merging.

Contract §5:

1. inside a bucket, split by a target byte budget with `i^(1/3)` growth so the first
   shard (the one a page hits first) is small and later shards get progressively larger;
2. then merge every layout constraint so that the characters of one GSUB/GPOS rule
   end up in a single shard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil, floor

from .layout_deps import Constraint
from .scanner import Bucket

FIRST_SHARD_DIVISOR = 5
MIN_ORDER_WIDTH = 3


@dataclass(slots=True)
class Shard:
    """One output file: a bundle of codepoints plus its build result."""

    area: str
    tag: str
    codes: list[int]
    origin: str = "primary"
    index: int = -1
    predicted_bytes: int = 0
    unmapped_deps: int = 0
    file: str = ""
    size: int = 0
    digest: str = ""
    glyphs: int = 0
    cmap_pruned: bool = False

    @property
    def chars(self) -> int:
        return len(self.codes)


@dataclass(slots=True)
class MergeReport:
    constraints: int = 0
    applied: int = 0
    chars_moved: int = 0
    emptied: int = 0
    attributed: int = 0


def shard_lengths(total: int, chars_per_shard: float) -> list[int]:
    """`i^(1/3)` growth: first shard 1/5 of the budget, then rising towards the cap."""
    if total <= 0:
        return []
    cap = floor(max(1.0, float(chars_per_shard)))
    quantum = cap / FIRST_SHARD_DIVISOR
    lengths: list[int] = []
    position = 0
    step = 1
    while position < total:
        want = min(ceil(quantum * step ** (1 / 3)), cap)
        length = max(1, min(int(want), total - position))
        lengths.append(length)
        position += length
        step += 1
    return lengths


def plan(buckets: tuple[Bucket, ...], density, chunk_size: int) -> list[Shard]:
    """Split every bucket into shards; bucket order is the shard priority order."""
    shards: list[Shard] = []
    for bucket in buckets:
        bytes_per_char = density.density(bucket.area)
        chars_per_shard = chunk_size / bytes_per_char
        position = 0
        for length in shard_lengths(len(bucket.codes), chars_per_shard):
            codes = list(bucket.codes[position : position + length])
            position += length
            shards.append(
                Shard(
                    area=bucket.area,
                    tag=bucket.tag,
                    codes=codes,
                    predicted_bytes=round(len(codes) * bytes_per_char),
                )
            )
    return shards


def merge_constraints(
    shards: list[Shard], constraints: list[Constraint]
) -> tuple[list[Shard], MergeReport]:
    """Force each constraint's characters into the first shard that already holds one.

    Characters are *moved* (never duplicated), so the "one character, one shard"
    invariant survives; shards emptied by the move are dropped. Returns the surviving
    shards in order (the input list is mutated in place).
    """
    report = MergeReport(constraints=len(constraints))
    for shard in shards:
        shard.unmapped_deps = 0
    if not shards or not constraints:
        return shards, report

    owner: dict[int, int] = {}
    for position, shard in enumerate(shards):
        for code in shard.codes:
            owner[code] = position

    members: list[set[int]] = [set(shard.codes) for shard in shards]

    for constraint in constraints:
        hits = {owner[code] for code in constraint.chars if code in owner}
        if not hits:
            continue
        target = min(hits)
        report.attributed += 1
        shards[target].unmapped_deps += constraint.unmapped
        sources = sorted(hits - {target})
        if not sources:
            continue
        report.applied += 1
        moved: list[int] = []
        for position in sources:
            present = members[position] & constraint.chars
            if not present:
                continue
            for code in sorted(present):
                moved.append(code)
                owner[code] = target
            members[position].difference_update(present)
            # keep the ordered list in sync with the set: a later constraint could
            # otherwise append a character that is still sitting in this list, and the
            # shard would end up holding it twice
            shards[position].codes = [
                code for code in shards[position].codes if code not in present
            ]
        moved.sort()
        members[target].update(moved)
        shards[target].codes.extend(moved)
        report.chars_moved += len(moved)

    ordered: list[Shard] = []
    for shard, codes in zip(shards, members):
        if not codes:
            report.emptied += 1
            continue
        if shard.tag in {"cjk-ext", "misc"}:
            shard.codes = sorted(codes)  # codepoint areas stay sorted
        ordered.append(shard)
    return ordered, report


def assign_indexes(shards: list[Shard]) -> None:
    for index, shard in enumerate(shards):
        shard.index = index


def order_width(count: int) -> int:
    return max(MIN_ORDER_WIDTH, len(str(max(count - 1, 0))))


@dataclass(slots=True)
class Plan:
    shards: list[Shard] = field(default_factory=list)
    merge: MergeReport = field(default_factory=MergeReport)

    @property
    def predicted_bytes(self) -> int:
        return sum(shard.predicted_bytes for shard in self.shards)
