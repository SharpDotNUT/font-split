"""Build orchestration for sharded font families."""

from __future__ import annotations

import os
import re
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from glob import escape as glob_escape
from pathlib import Path

from fontTools.ttLib import TTFont
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)

from . import verify as verify_module
from .areas import AreaTable, load_area_table
from .config import BuildConfig, slugify, tool_version
from .css import Face, render_css
from .density import Calibration, calibrate, closure_scale
from .encoder import (
    blake2b8,
    blake2b8_file,
    encode_woff2,
    glyph_count,
    prune_cmap,
    sha256,
    to_sfnt,
)
from .errors import FontSplitError
from .layout_deps import Constraint, collect_constraints, restrict_constraints
from .manifest import build_manifest, load_manifest, shard_entry, write_manifest
from .planner import MergeReport, Shard, assign_indexes, merge_constraints, order_width
from .planner import plan as plan_shards
from .scanner import Bucketing, bucket_codepoints, scan_codepoints
from .subsetter import Subsetter
from .unicode_range import format_codes, merge_ranges

MAX_MERGE_PASSES = 4
MANIFEST_NAME = "manifest.json"
SHARD_SUFFIX = ".woff2"


@dataclass(slots=True)
class BuildResult:
    config: BuildConfig
    directory: Path
    font_family: str
    slug: str
    fingerprint: str
    weight: str
    style: str
    source_sha256: str
    source_bytes: int
    codepoints: list[int]
    area_table: AreaTable
    bucketing: Bucketing
    plan: list[Shard]
    merge: MergeReport
    constraints: list[Constraint]
    constraint_rules: int
    constraint_skipped: int
    calibration: Calibration
    fallback_codepoints: list[int] = field(default_factory=list)
    fallback_bucketing: Bucketing = field(
        default_factory=lambda: Bucketing((), 0, 0, ())
    )
    fallback_merge: MergeReport = field(default_factory=MergeReport)
    fallback_constraints: list[Constraint] = field(default_factory=list)
    fallback_source_sha256: str = ""
    fallback_source_bytes: int = 0
    summary: dict = field(default_factory=dict)
    manifest: dict = field(default_factory=dict)
    css: str = ""
    css_path: Path | None = None
    manifest_path: Path | None = None
    built: int = 0
    reused: int = 0
    stale_removed: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True, slots=True)
class _Job:
    shard: Shard
    directory: Path
    subsetter: Subsetter


def load_source(path: Path, font_number: int = 0) -> tuple[bytes, TTFont]:
    """Load a font as HarfBuzz-readable sfnt bytes and a fontTools face."""
    if not path.is_file():
        raise FontSplitError(f"source font not found: {path}")
    try:
        font = TTFont(str(path), fontNumber=font_number, recalcTimestamp=False)
    except Exception as exc:
        raise FontSplitError(f"cannot read font {path}: {exc}") from exc
    return to_sfnt(path.read_bytes()), font


def font_family_name(font: TTFont) -> str:
    name = font.get("name")
    if name is None:
        raise FontSplitError("font has no 'name' table")
    for name_id in (16, 1):
        value = name.getDebugName(name_id)
        if value:
            return value
    raise FontSplitError("font 'name' table has no family name")


def font_weight_and_style(font: TTFont) -> tuple[str, str]:
    """Read the variable weight range or static OS/2 weight."""
    weight = None
    fvar = font.get("fvar")
    if fvar is not None:
        axis = {axis.axisTag: axis for axis in fvar.axes}.get("wght")
        if axis is not None:
            low, high = round(axis.minValue), round(axis.maxValue)
            weight = str(low) if low == high else f"{low} {high}"
    if weight is None:
        os2 = font.get("OS/2")
        weight = (
            str(os2.usWeightClass) if os2 is not None and os2.usWeightClass else "400"
        )
    head = font.get("head")
    return weight, "italic" if head is not None and head.macStyle & 0x02 else "normal"


def build(config: BuildConfig, console: Console) -> BuildResult:
    raw, font = load_source(config.source, config.font_number)
    fallback_raw, fallback_font = (b"", None)
    if config.fallback is not None:
        fallback_raw, fallback_font = load_source(config.fallback)
    area_table = load_area_table(config.areas)
    codepoints = scan_codepoints(font)
    bucketing = bucket_codepoints(
        codepoints, area_table, include_cjk_ext=config.include_cjk_ext
    )
    family = config.css_family or font_family_name(font)
    slug = config.slug or slugify(family)
    weight, style = font_weight_and_style(font)
    source_sha = sha256(raw)
    fallback_sha = sha256(fallback_raw) if fallback_raw else None
    fingerprint = config.fingerprint(
        source_sha256=source_sha,
        fallback_sha256=fallback_sha,
        areas_version=area_table.version,
    )
    result = BuildResult(
        config=config,
        directory=config.output / slug,
        font_family=family,
        slug=slug,
        fingerprint=fingerprint,
        weight=weight,
        style=style,
        source_sha256=source_sha,
        source_bytes=len(raw),
        codepoints=codepoints,
        area_table=area_table,
        bucketing=bucketing,
        plan=[],
        merge=MergeReport(),
        constraints=[],
        constraint_rules=0,
        constraint_skipped=0,
        calibration=Calibration(per_area={}, globals=8.0, samples={}),
        fallback_source_sha256=fallback_sha or "",
        fallback_source_bytes=len(fallback_raw),
    )
    console.print(
        f"[bold]{family}[/] ({slug}) · {len(codepoints):,} codepoints · {len(bucketing.buckets)} non-empty areas · fingerprint {fingerprint}"
    )

    if not config.dry_run:
        subsetter = Subsetter(raw, config.features)
        with _spinner(console, "calibrating bytes/char per area") as progress:
            progress.add_task("calibrate")
            result.calibration = calibrate(
                bucketing.buckets,
                lambda codes: len(encode_woff2(subsetter.subset(codes))),
            )
    shards = plan_shards(bucketing.buckets, result.calibration, config.chunk_size)
    report = collect_constraints(font, features=config.features, mode=config.deps)
    result.constraints = list(report.constraints)
    result.constraint_rules = report.rules
    result.constraint_skipped = report.skipped
    result.plan, result.merge = _merge_all(shards, result.constraints)
    _discount_prediction(result.plan, result.calibration, result.merge.chars_moved)

    if fallback_font is not None:
        missing = sorted(set(scan_codepoints(fallback_font)) - set(codepoints))
        result.fallback_bucketing = bucket_codepoints(
            missing, area_table, include_cjk_ext=config.include_cjk_ext
        )
        fallback_calibration = Calibration(per_area={}, globals=8.0, samples={})
        if not config.dry_run and result.fallback_bucketing.buckets:
            fallback_subsetter = Subsetter(fallback_raw, config.features)
            with _spinner(
                console, "calibrating fallback bytes/char per area"
            ) as progress:
                progress.add_task("calibrate")
                fallback_calibration = calibrate(
                    result.fallback_bucketing.buckets,
                    lambda codes: len(encode_woff2(fallback_subsetter.subset(codes))),
                )
        fallback_shards = plan_shards(
            result.fallback_bucketing.buckets, fallback_calibration, config.chunk_size
        )
        for shard in fallback_shards:
            shard.origin = "fallback"
        fallback_report = collect_constraints(
            fallback_font, features=config.features, mode=config.deps
        )
        allowed = [
            code
            for bucket in result.fallback_bucketing.buckets
            for code in bucket.codes
        ]
        result.fallback_constraints = list(
            restrict_constraints(fallback_report.constraints, allowed)
        )
        fallback_shards, result.fallback_merge = _merge_all(
            fallback_shards, result.fallback_constraints
        )
        _discount_prediction(
            fallback_shards, fallback_calibration, result.fallback_merge.chars_moved
        )
        result.fallback_codepoints = sorted(
            code
            for bucket in result.fallback_bucketing.buckets
            for code in bucket.codes
        )
        result.plan.extend(fallback_shards)

    assign_indexes(result.plan)
    del font, fallback_font
    if config.dry_run:
        result.summary = _summary(result, actual=None, baseline=None)
        return result
    result.directory.mkdir(parents=True, exist_ok=True)
    _build_shards(result, raw, fallback_raw, console)
    result.problems = _self_check(result, raw)
    return result


def _merge_all(
    shards: list[Shard], constraints: list[Constraint]
) -> tuple[list[Shard], MergeReport]:
    total = MergeReport(constraints=len(constraints))
    for _ in range(MAX_MERGE_PASSES):
        shards, report = merge_constraints(shards, constraints)
        total.applied += report.applied
        total.chars_moved += report.chars_moved
        total.emptied += report.emptied
        total.attributed = report.attributed
        if report.applied == 0:
            break
    return shards, total


def _discount_prediction(
    shards: list[Shard], calibration: Calibration, chars_moved: int
) -> None:
    scale = closure_scale(
        calibration.closure_fraction,
        chars_moved,
        sum(shard.chars for shard in shards),
    )
    if scale == 1.0:
        return
    for shard in shards:
        shard.predicted_bytes = max(1, round(shard.predicted_bytes * scale))


def _build_shards(
    result: BuildResult, raw: bytes, fallback_raw: bytes, console: Console
) -> None:
    config = result.config
    shards = result.plan
    width = order_width(len(shards))
    existing = (
        {}
        if config.force
        else _existing_shards(result.directory, result.slug, result.fingerprint)
    )
    subsetters = {
        "primary": Subsetter(raw, config.features),
        "fallback": Subsetter(fallback_raw, config.features) if fallback_raw else None,
    }
    pending: list[_Job] = []
    for shard in shards:
        found = existing.get(shard.index)
        if found is not None:
            name, path, digest, origin = found
            if origin == shard.origin and blake2b8_file(path) == digest:
                shard.file, shard.digest = name, digest
                shard.size, shard.glyphs = (
                    path.stat().st_size,
                    glyph_count(path.read_bytes()),
                )
                result.reused += 1
                continue
        subsetter = subsetters[shard.origin]
        if subsetter is None:
            raise FontSplitError("fallback shard has no fallback source font")
        pending.append(_Job(shard, result.directory, subsetter))
    baseline_future: Future | None = None
    fallback_baseline_future: Future | None = None
    with ThreadPoolExecutor(max_workers=config.jobs) as pool:
        if not config.skip_baseline:
            baseline_future = pool.submit(encode_woff2, raw)
            if fallback_raw and result.fallback_codepoints:
                fallback_baseline_future = pool.submit(encode_woff2, fallback_raw)
        futures = [pool.submit(_build_one, job, result.slug, width) for job in pending]
        if futures:
            with _bar(console, "subsetting + encoding") as progress:
                task = progress.add_task("shards", total=len(futures))
                for future in futures:
                    future.result()
                    progress.advance(task)
        baseline = baseline_future.result() if baseline_future else None
        fallback_baseline = (
            fallback_baseline_future.result() if fallback_baseline_future else None
        )
    result.built = len(pending)
    result.stale_removed = _drop_stale(
        result.directory, result.slug, [shard.file for shard in shards]
    )
    result.summary = _summary(
        result,
        actual=sum(shard.size for shard in shards),
        baseline=len(baseline) if baseline else None,
        fallback_baseline=len(fallback_baseline) if fallback_baseline else None,
    )
    _write_outputs(result)


def _build_one(job: _Job, slug: str, width: int) -> None:
    shard = job.shard
    subset = job.subsetter.subset(shard.codes)
    pruned = prune_cmap(subset, shard.codes)
    shard.cmap_pruned = pruned is not subset
    woff2 = encode_woff2(pruned)
    digest = blake2b8(woff2)
    name = _shard_name(slug, shard, width, digest)
    path = job.directory / name
    part = path.with_name(name + ".part")
    part.write_bytes(woff2)
    os.replace(part, path)
    shard.file, shard.digest, shard.size, shard.glyphs = (
        name,
        digest,
        len(woff2),
        glyph_count(subset),
    )


def _fallback_manifest(result: BuildResult) -> dict | None:
    if result.config.fallback is None:
        return None
    return {
        "source": {
            "file": result.config.fallback.name,
            "sha256": result.fallback_source_sha256,
            "bytes": result.fallback_source_bytes,
        },
        "codepoints": len(result.fallback_codepoints),
        "shards": sum(shard.origin == "fallback" for shard in result.plan),
    }


def _write_outputs(result: BuildResult) -> None:
    result.css = render_css(
        family=result.font_family,
        faces=[Face(s.file, format_codes(s.codes)) for s in result.plan],
        weight=result.weight,
        style=result.style,
        header=f"{result.font_family} | build {result.fingerprint} | {len(result.plan)} shards | generated by font-sharder",
    )
    result.css_path = result.directory / f"{result.slug}.css"
    result.css_path.write_text(result.css, encoding="utf-8")
    result.manifest = build_manifest(
        font_family=result.font_family,
        slug=result.slug,
        fingerprint=result.fingerprint,
        source={
            "file": result.config.source.name,
            "sha256": result.source_sha256,
            "bytes": result.source_bytes,
        },
        fallback=_fallback_manifest(result),
        tool={"name": "font-sharder", "version": tool_version()},
        config={
            **result.config.manifest_config(),
            "areas": result.area_table.version,
            "excludedCjkExt": len(result.bucketing.dropped),
        },
        summary=result.summary,
        shards=[shard_entry(shard) for shard in result.plan],
    )
    result.manifest_path = result.directory / MANIFEST_NAME
    write_manifest(result.manifest_path, result.manifest)


def _self_check(result: BuildResult, raw: bytes) -> list[str]:
    if not result.config.verify:
        return []
    problems: list[str] = []
    problems += verify_module.check_coverage(
        result.codepoints + result.fallback_codepoints,
        result.plan,
        excluded=list(result.bucketing.dropped)
        + list(result.fallback_bucketing.dropped),
    )
    problems += verify_module.check_mutex(result.plan)
    problems += verify_module.check_ranges(result.plan, result.directory)
    problems += verify_module.check_constraints(
        result.constraints + result.fallback_constraints, result.plan
    )
    problems += verify_module.check_artifacts(result.manifest, result.directory)
    problems += verify_module.check_fonts(
        result.plan, result.directory, source_font=raw
    )
    problems += verify_module.check_css(result.css, result.plan, result.directory)
    problems += verify_module.check_size_baseline(
        result.summary["predictedBytes"], result.summary["bytes"]
    )
    return problems


def _shard_name(slug: str, shard: Shard, width: int, digest: str) -> str:
    return f"{slug}-{shard.index:0{width}d}-{shard.tag}.{digest}{SHARD_SUFFIX}"


def _existing_shards(
    directory: Path, slug: str, fingerprint: str
) -> dict[int, tuple[str, Path, str, str]]:
    manifest = directory / MANIFEST_NAME
    if not manifest.is_file():
        return {}
    try:
        payload = load_manifest(manifest)
    except (OSError, ValueError):
        return {}
    if payload.get("fingerprint") != fingerprint:
        return {}
    pattern = re.compile(
        rf"^{re.escape(slug)}-(\d+)-([A-Za-z0-9-]+)\.([0-9a-f]{{16}}){re.escape(SHARD_SUFFIX)}$"
    )
    found: dict[int, tuple[str, Path, str, str]] = {}
    entries = payload.get("shards", [])
    for path in directory.iterdir():
        match = pattern.match(path.name) if path.is_file() else None
        if match is None:
            continue
        index = int(match.group(1))
        origin = (
            entries[index].get("origin", "primary")
            if index < len(entries)
            else "primary"
        )
        found[index] = (path.name, path, match.group(3), origin)
    return found


def _drop_stale(directory: Path, slug: str, keep: list[str]) -> int:
    wanted = set(keep)
    stale = [
        path
        for path in directory.glob(f"{glob_escape(slug)}-*{SHARD_SUFFIX}")
        if path.name not in wanted
    ]
    for path in stale:
        path.unlink(missing_ok=True)
    return len(stale)


def _summary(
    result: BuildResult,
    *,
    actual: int | None,
    baseline: int | None,
    fallback_baseline: int | None = None,
) -> dict:
    predicted = sum(shard.predicted_bytes for shard in result.plan)
    fallback = [shard for shard in result.plan if shard.origin == "fallback"]
    summary = {
        "shards": len(result.plan),
        "bytes": actual if actual is not None else predicted,
        "chars": sum(shard.chars for shard in result.plan),
        "cssSegments": sum(len(merge_ranges(shard.codes)) for shard in result.plan),
        "singleFileWoff2Bytes": baseline,
        "predictedBytes": predicted,
        "areas": len(result.bucketing.buckets),
        "excludedCjkExt": len(result.bucketing.dropped),
        "constraints": len(result.constraints),
        "constraintRules": result.constraint_rules,
        "constraintRulesSkipped": result.constraint_skipped,
        "constraintsMerged": result.merge.applied,
        "constraintCharsMoved": result.merge.chars_moved,
        "shardsEmptied": result.merge.emptied,
        "cmapPruned": sum(shard.cmap_pruned for shard in result.plan),
        "staleRemoved": result.stale_removed,
        "fallbackShards": len(fallback),
        "fallbackChars": sum(shard.chars for shard in fallback),
        "fallbackBytes": sum(shard.size or shard.predicted_bytes for shard in fallback),
        "fallbackSingleFileWoff2Bytes": fallback_baseline,
        "fallbackConstraints": len(result.fallback_constraints),
    }
    if baseline:
        summary["expansionRatio"] = round(summary["bytes"] / baseline, 4)
    return summary


def _bar(console: Console, label: str) -> Progress:
    return Progress(
        TextColumn(label),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )


def _spinner(console: Console, label: str) -> Progress:
    return Progress(
        TextColumn(label), TimeElapsedColumn(), console=console, transient=True
    )
