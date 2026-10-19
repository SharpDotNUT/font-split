# Repository Guidelines

`font-split` is a standalone Python distribution and CLI that pre-shards one font into Unicode-aware WOFF2 files. It is also a git submodule of `fonts-cdn`, where the parent workspace supplies the environment and `fonts-build` calls the same package.

`AGENTS.md` is English. `README.md` and every file under `docs/` are Chinese; keep new documentation in Chinese.

## Layout

| Path | Role |
|---|---|
| `src/font_split/build.py` | End-to-end shard build, fallback planning, outputs |
| `src/font_split/config.py` | `BuildConfig`, fingerprint, `SHARD_PIPELINE` |
| `src/font_split/scanner.py` | cmap scan and area bucketing |
| `src/font_split/areas.py` | Vendored language-area table |
| `src/font_split/density.py` | Per-area byte calibration and closure discount |
| `src/font_split/planner.py` | `i^(1/3)` split and constraint merge |
| `src/font_split/layout_deps.py` | GSUB/GPOS co-location extraction |
| `src/font_split/subsetter.py` | Per-thread HarfBuzz subsetter |
| `src/font_split/encoder.py` | Deterministic WOFF2 encoding and hashes |
| `src/font_split/manifest.py` | Schema 2 manifest |
| `src/font_split/verify.py` | Eight artifact contract checks |
| `src/font_split/cli.py` | `font-sharder` CLI |
| `tools/gen_lang_data.py` | Regenerates `data/lang-unicodes.json` |
| `docs/` | Artifact contract, implementation notes, background research |

## Commands

```bash
uv sync
uv run font-sharder <font.ttf> -o dist --css-family "Test"
uv run font-sharder <font.ttf> -o dist --css-family "Test"   # reuse check
uv run --group dev python tools/gen_lang_data.py
uv tool run ruff check src tools
uv tool run ruff format --check src tools
```

Python ≥3.11; use uv. There is no unit-test suite: a dry run, one real build, and a repeat reuse run are the verification gate. Generated shard bytes must stay deterministic (`recalcTimestamp=False`). Increment `SHARD_PIPELINE` whenever output bytes change without a user-facing option changing.

## Contract notes

* Shards are exclusive and complete; CSS derives `unicode-range` from each shard's planned codepoints.
* `--fallback` ships `fallback cmap - primary cmap`; fallback shards follow primary shards and carry `origin = "fallback"`.
* Per-origin layout constraints are enforced; constraints spanning both origins are not required to share a shard.
* Large class/context groups above `MAX_GROUP_CHARS` are skipped and rely on the per-shard subset closure.
* Reuse is gated on the build fingerprint, including source and fallback digests, byte-affecting configuration, and `SHARD_PIPELINE`.
* Upload is not implemented; manifest shard entries are the upload and integrity checklist.

## Editing from `fonts-cdn`

When this directory is a submodule, commit and push changes here first, then update the gitlink in the parent repository. Do not edit through the parent repository's index.
