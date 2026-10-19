"""Per-area byte density calibration.

Shard sizing is expressed in bytes, but a bucket holds characters, so every area is
sampled and subset+encoded for real to learn its own bytes-per-character. A single
global average would be badly wrong (Latin with a wide feature closure is ~5x denser
than CJK), which is exactly the defect this step removes.

That sample rate still includes subset-closure overhead. A larger probe of the
biggest bucket measures how much of the rate disappears once the closure is shared;
constraint merge uses that fraction to stop pricing moved characters as their own
subset.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .scanner import Bucket

DEFAULT_SAMPLE_SIZE = 192
CLOSURE_PROBE_FACTOR = 4
MIN_BYTES_PER_CHAR = 8.0


@dataclass(frozen=True, slots=True)
class Calibration:
    per_area: dict[str, float]
    globals: float
    samples: dict[str, int]
    closure_fraction: float = 0.0

    def density(self, area: str) -> float:
        return self.per_area.get(area, self.globals)


def sample_codes(codes: Sequence[int], size: int = DEFAULT_SAMPLE_SIZE) -> list[int]:
    """Evenly spaced sample that still reaches into the tail of a large bucket."""
    if len(codes) <= size:
        return list(codes)
    stride = len(codes) / size
    picked = [codes[min(int(index * stride), len(codes) - 1)] for index in range(size)]
    return sorted(set(picked))


def calibrate(
    buckets: Sequence[Bucket],
    measure: Callable[[Sequence[int]], int],
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> Calibration:
    """Measure bytes/char for every bucket, plus a global fallback."""
    per_area: dict[str, float] = {}
    samples: dict[str, int] = {}
    largest: Bucket | None = None
    largest_bytes = 0
    largest_sample_n = 0
    for bucket in buckets:
        sample = sample_codes(bucket.codes, sample_size)
        if not sample:
            continue
        size = measure(sample)
        per_area[bucket.area] = max(size / len(sample), MIN_BYTES_PER_CHAR)
        samples[bucket.area] = len(sample)
        if largest is None or len(bucket.codes) > len(largest.codes):
            largest = bucket
            largest_bytes = size
            largest_sample_n = len(sample)

    if per_area:
        globals_ = statistics.median(per_area.values())
    else:  # pragma: no cover - only reachable with an empty font
        globals_ = MIN_BYTES_PER_CHAR
    closure = _closure_fraction(
        largest,
        sample_bytes=largest_bytes,
        sample_n=largest_sample_n,
        measure=measure,
        sample_size=sample_size,
    )
    return Calibration(
        per_area=per_area,
        globals=globals_,
        samples=samples,
        closure_fraction=closure,
    )


def closure_scale(closure_fraction: float, chars_moved: int, total_chars: int) -> float:
    """Scale a pre-merge byte prediction after characters share an existing subset.

    ``closure_fraction`` is the part of the sample rate that is closure overhead
    rather than per-character glyph cost. Only the moved share of the plan was
    priced as its own subset and then encoded inside another shard.
    """
    if total_chars <= 0 or chars_moved <= 0 or closure_fraction <= 0:
        return 1.0
    moved = min(1.0, chars_moved / total_chars)
    return 1.0 - closure_fraction * moved


def _closure_fraction(
    bucket: Bucket | None,
    *,
    sample_bytes: int,
    sample_n: int,
    measure: Callable[[Sequence[int]], int],
    sample_size: int,
) -> float:
    if bucket is None or sample_n < 2 or sample_bytes <= 0:
        return 0.0
    large_n = min(len(bucket.codes), max(sample_size * CLOSURE_PROBE_FACTOR, 512))
    if large_n < sample_n * 2:
        return 0.0
    large = sample_codes(bucket.codes, large_n)
    if len(large) < sample_n * 2:
        return 0.0
    large_bytes = measure(large)
    if large_bytes <= 0:
        return 0.0
    small_rate = sample_bytes / sample_n
    large_rate = large_bytes / len(large)
    return max(0.0, 1.0 - large_rate / small_rate)
