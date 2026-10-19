"""`font-sharder` command line interface."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .build import BuildResult, build
from .config import DEFAULT_CHUNK_SIZE, DEFAULT_FEATURES, BuildConfig, tool_version
from .errors import ConfigError, FontSplitError
from .layout_deps import LOOKUP_MODES
from .manifest import write_manifest
from .subsetter import feature_tag
from .unicode_range import merge_ranges
from .util import human_bytes

VERBOSE_ROWS = 400


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="font-sharder",
        description=(
            "Pre-split a font into Unicode-aware WOFF2 shards and emit the matching "
            "CSS + manifest for a static font CDN."
        ),
    )
    parser.add_argument(
        "source", type=Path, help="input font: .ttf/.otf/.woff/.woff2 (and .ttc)"
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("dist"),
        help="output root (default: dist); files land in <output>/<slug>/",
    )
    parser.add_argument(
        "--family",
        default=None,
        help="family slug / file prefix (default: slugified name table)",
    )
    parser.add_argument(
        "--css-family",
        default=None,
        help="CSS font-family string (default: name table)",
    )
    parser.add_argument(
        "--fallback",
        type=Path,
        default=None,
        help="full-coverage font used for codepoints missing from the primary source",
    )
    parser.add_argument(
        "-s",
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help="target WOFF2 bytes per shard",
    )
    parser.add_argument(
        "-F",
        "--features",
        default=",".join(DEFAULT_FEATURES),
        help="comma separated OpenType features to retain; 'all' keeps every feature, '' keeps none",
    )
    parser.add_argument(
        "--deps",
        choices=LOOKUP_MODES,
        default="lookup",
        help="co-location granularity for layout rules",
    )
    parser.add_argument(
        "--include-cjk-ext",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="include CJK extension blocks (default: include)",
    )
    parser.add_argument(
        "--areas", type=Path, default=None, help="alternative area table JSON"
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=os.cpu_count() or 4, help="parallel workers"
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="rebuild shards even if they already exist",
    )
    parser.add_argument(
        "--skip-baseline",
        action="store_true",
        help="skip the single-file WOFF2 size comparison",
    )
    parser.add_argument(
        "--no-verify", action="store_true", help="skip the contract self checks"
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="plan shards and print them, build nothing",
    )
    parser.add_argument(
        "--manifest-out",
        type=Path,
        default=None,
        help="in --dry-run, write the predicted manifest here",
    )
    parser.add_argument(
        "--font-number", type=int, default=0, help="font index inside a .ttc collection"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="print the full shard table"
    )
    return parser


def parse_features(value: str) -> tuple[str, ...] | None:
    """`all` -> None (keep everything); empty -> () (keep none); otherwise a whitelist."""
    text = value.strip()
    if text.lower() == "all":
        return None
    if not text:
        return ()
    names = [part.strip() for part in text.split(",") if part.strip()]
    for name in names:
        feature_tag(name)  # validates length and encoding
    return tuple(dict.fromkeys(names))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console()

    if args.chunk_size < 1:
        console.print("[red]error:[/] --chunk-size must be >= 1")
        return 1
    if args.jobs < 1:
        console.print("[red]error:[/] --jobs must be >= 1")
        return 1
    try:
        features = parse_features(args.features)
    except ValueError as exc:
        console.print(f"[red]error:[/] {escape(str(exc))}")
        return 1

    config = _config(args, features)
    try:
        result = build(config, console)
    except (FontSplitError, ConfigError) as exc:
        console.print(f"[red]error:[/] {escape(str(exc))}")
        return 1
    except KeyboardInterrupt:
        console.print("[yellow]interrupted[/]")
        return 130

    _report(result, console, verbose=args.verbose, dry_run=args.dry_run)
    if args.dry_run and args.manifest_out is not None:
        write_manifest(
            args.manifest_out, result.manifest or _predicted_manifest(result)
        )
        console.print(f"predicted manifest → {escape(str(args.manifest_out))}")
    return 0 if result.ok else 2


def _config(args: argparse.Namespace, features) -> BuildConfig:
    return BuildConfig(
        source=args.source,
        output=args.output,
        slug=args.family,
        css_family=args.css_family,
        fallback=args.fallback,
        chunk_size=args.chunk_size,
        features=features,
        deps=args.deps,
        include_cjk_ext=args.include_cjk_ext,
        areas=args.areas,
        jobs=args.jobs,
        force=args.force,
        skip_baseline=args.skip_baseline,
        dry_run=args.dry_run,
        verify=not args.no_verify,
        font_number=args.font_number,
    )


def _report(
    result: BuildResult, console: Console, *, verbose: bool, dry_run: bool
) -> None:
    summary = result.summary
    console.print(
        f"{escape(result.font_family)} → [bold]{escape(str(result.directory))}[/]"
        + (" [dim](dry run)[/]" if dry_run else "")
    )
    if dry_run or verbose:
        _table(result, console)

    numbers = [
        f"{summary['shards']} shards",
        f"{human_bytes(summary['bytes'])}" + (" predicted" if dry_run else ""),
        f"{summary['chars']:,} chars",
        f"{summary['cssSegments']:,} unicode-range segments",
    ]
    if summary.get("singleFileWoff2Bytes"):
        ratio = summary.get("expansionRatio")
        numbers.append(
            f"single file {human_bytes(summary['singleFileWoff2Bytes'])}"
            + (f" (x{ratio:.2f}, {ratio - 1:+.1%})" if ratio else "")
        )
    if not dry_run:
        numbers.append(f"built {result.built}, reused {result.reused}")
        if result.stale_removed:
            numbers.append(f"removed {result.stale_removed} stale file(s)")
    console.print("  " + " · ".join(numbers))
    console.print(
        f"  merge: {summary['constraints']} constraints, {summary['constraintsMerged']} applied, "
        f"{summary['constraintCharsMoved']:,} chars moved, {summary['shardsEmptied']} shard(s) emptied"
    )
    if summary.get("cmapPruned"):
        console.print(
            f"  cmap: pruned {summary['cmapPruned']} shard(s) to their unicode-range "
            "(HarfBuzz keeps mappings of retained glyphs)"
        )

    for problem in result.problems:
        console.print(f"[red]✘[/] {escape(problem)}")
    if result.problems:
        console.print(f"[red]{len(result.problems)} contract check(s) failed[/]")
    elif not dry_run:
        console.print("[green]✔[/] contract checks passed")


def _table(result: BuildResult, console: Console) -> None:
    table = Table(show_edge=False, pad_edge=False)
    for column in (
        "order",
        "area",
        "tag",
        "chars",
        "bytes",
        "segments",
        "file",
        "unicode-range",
    ):
        table.add_column(column, overflow="fold")
    rows = (
        result.plan if len(result.plan) <= VERBOSE_ROWS else result.plan[:VERBOSE_ROWS]
    )
    for shard in rows:
        table.add_row(
            f"{shard.index:03d}",
            escape(shard.area),
            escape(shard.tag),
            f"{shard.chars:,}",
            human_bytes(shard.size or shard.predicted_bytes),
            f"{len(merge_ranges(shard.codes)):,}",
            escape(shard.file),
            escape(_range_preview(shard)),
        )
    console.print(table)
    if len(rows) != len(result.plan):
        console.print(f"[dim]... {len(result.plan) - len(rows)} more shards[/]")


def _range_preview(shard) -> str:
    codes = sorted(shard.codes)
    head = ",".join(f"U+{code:04X}" for code in codes[:3])
    return f"{head},..." if len(codes) > 3 else head


def _predicted_manifest(result: BuildResult) -> dict:
    from .manifest import build_manifest, shard_entry

    fallback = None
    if result.config.fallback is not None:
        fallback = {
            "source": {
                "file": result.config.fallback.name,
                "sha256": result.fallback_source_sha256,
                "bytes": result.fallback_source_bytes,
            },
            "codepoints": len(result.fallback_codepoints),
            "shards": sum(shard.origin == "fallback" for shard in result.plan),
        }
    return build_manifest(
        font_family=result.font_family,
        slug=result.slug,
        fingerprint=result.fingerprint,
        source={
            "file": result.config.source.name,
            "sha256": result.source_sha256,
            "bytes": result.source_bytes,
        },
        fallback=fallback,
        tool={"name": "font-sharder", "version": tool_version()},
        config=result.config.manifest_config(),
        summary=result.summary,
        shards=[shard_entry(shard) for shard in result.plan],
    )


if __name__ == "__main__":
    sys.exit(main())
