#!/usr/bin/env python
"""Regenerate the vendored language-area / character-frequency data.

The area table and the Chinese frequency list are the core asset of the sharder:
they inject "which characters are common in which language" into the shard layout.
They are derived from cn-font-split (Apache-2.0, see NOTICE) and vendored as
`src/font_split/data/lang-unicodes.json` so that builds are reproducible
and offline.

Usage:
    uv run --group dev python tools/gen_lang_data.py [--ref REF] [--out PATH]

`--ref` defaults to the pinned upstream commit recorded in this file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

REPO = "KonghaYao/cn-font-split"
DEFAULT_REF = "1650af130b8c547b13a883ac7546f088ff193bf5"
RAW = f"https://raw.githubusercontent.com/{REPO}/{{ref}}/{{path}}"

SOURCES = {
    "lib.rs": "crates/lang_unicodes/src/lib.rs",
    "cjk_unicodes.rs": "crates/lang_unicodes/src/cjk_unicodes.rs",
    "build.rs": "crates/lang_unicodes/build.rs",
    "syl-2.txt": "crates/lang_unicodes/scripts/syl-2.txt",
}

DATA_VERSION = "lang-unicodes@1"
OUT_DEFAULT = Path("src/font_split/data/lang-unicodes.json")

#: area name -> shard tag from the artifact contract (unknown areas fall back to
#: a slug of the area name, which keeps the vocabulary open for other scripts).
TAGS = {
    "LATIN": "latin",
    "LATIN_1": "latin1",
    "LATIN_EXT_A": "latinext",
    "LATIN_EXT_B": "latinext",
    "IPA_SYMBOLS": "ipa",
    "GREEK": "greek",
    "CYRILLIC": "cyrillic",
    "ZH_SYMBOL": "sym",
    "ZH_SC": "han",
    "ZH_TC": "han-t",
    "HIRAGANA_AND_KATAKANA": "kana",
    "HANGUL_JAMO": "jamo",
    "HANGUL_SYL": "hangul",
    "HALFWIDTH_FULLWIDTH": "full",
}

#: areas whose characters are stored as an ordered list instead of ranges
FREQUENCY_AREAS = {"ZH_SC", "ZH_TC", "HANGUL_SYL"}


def fetch(path: str, ref: str, cache: Path) -> str:
    target = cache / f"{ref[:12]}-{path.replace('/', '_')}"
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        url = RAW.format(ref=ref, path=path)
        print(f"fetching {url}", file=sys.stderr)
        with urllib.request.urlopen(url, timeout=60) as response:
            target.write_bytes(response.read())
    return target.read_text(encoding="utf-8")


def rust_str_literal(text: str, name: str) -> str:
    """Extract a Rust `const NAME: &str = "..."` literal, decoding escapes."""
    marker = re.search(rf"const\s+{re.escape(name)}\s*:\s*&str\s*=\s*\"", text)
    if marker is None:
        raise SystemExit(f"literal {name!r} not found")
    out: list[str] = []
    pos = marker.end()
    while pos < len(text):
        char = text[pos]
        if char == "\\":
            escape = text[pos + 1]
            if escape == "u":
                brace = text.index("{", pos)
                end = text.index("}", brace)
                out.append(chr(int(text[brace + 1 : end], 16)))
                pos = end + 1
            else:
                out.append(
                    {"n": "\n", "r": "\r", "t": "\t", "0": "\0"}.get(escape, escape)
                )
                pos += 2
            continue
        if char == '"':
            return "".join(out)
        out.append(char)
        pos += 1
    raise SystemExit(f"unterminated literal {name!r}")


def expand_ranges(text: str, name: str) -> list[tuple[int, int]]:
    """Extract `expand_ranges(&[(0x.., 0x..), ...])` pairs for one static."""
    marker = re.search(rf"\b{re.escape(name)}\b[^=]*=\s*expand_ranges\(&\[", text)
    if marker is None:
        raise SystemExit(f"area {name!r} not found")
    end = text.index("]);", marker.end())
    body = text[marker.end() : end]
    pairs = [
        (int(start, 16), int(stop, 16))
        for start, stop in re.findall(
            r"\(\s*0x([0-9A-Fa-f]+)\s*,\s*0x([0-9A-Fa-f]+)\s*\)", body
        )
    ]
    if not pairs:
        raise SystemExit(f"area {name!r} has no ranges")
    return pairs


def area_order(lib: str) -> list[str]:
    signature = re.search(r"fn create_default_unicode_area_tag\(\)[^{]*\{", lib)
    if signature is None:
        raise SystemExit("create_default_unicode_area_tag() not found")
    body = lib[signature.end() :]
    body = body[body.index("[") : body.index("]")]
    names = re.findall(r'"([A-Z0-9_]+)"', body)
    if len(names) != 32:
        raise SystemExit(f"expected 32 areas, found {len(names)}")
    return names


def korean_syllables(text: str) -> list[str]:
    chunks = text.split("=====================================")
    if len(chunks) < 2:
        raise SystemExit("unexpected syl-2.txt layout")
    out: list[str] = []
    for line in chunks[1].splitlines()[1:]:
        token = line.split(" ")[0].strip()
        if token:
            out.append(token[0])
    return out


def to_simplified_to_traditional(chars: str) -> list[str]:
    from opencc import OpenCC

    converter = OpenCC("s2t")
    seen = set(chars)
    out: list[str] = []
    for char in chars:
        converted = converter.convert(char)
        # upstream stores the result as u16, so only BMP characters can be kept
        if len(converted) != 1 or ord(converted) > 0xFFFF or converted in seen:
            continue
        seen.add(converted)
        out.append(converted)
    return out


def slugify(name: str) -> str:
    return name.lower().replace("_", "-")


def unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ref", default=DEFAULT_REF, help="upstream git ref (commit/tag/branch)"
    )
    parser.add_argument("--out", type=Path, default=OUT_DEFAULT)
    parser.add_argument("--cache", type=Path, default=Path(".cache/upstream"))
    args = parser.parse_args(argv)

    sources = {
        name: fetch(path, args.ref, args.cache) for name, path in SOURCES.items()
    }
    lib = sources["lib.rs"]
    cjk = sources["cjk_unicodes.rs"]

    symbol = rust_str_literal(sources["build.rs"], "CN_SYMBOL")
    simplified = rust_str_literal(sources["build.rs"], "CN_CHARS")
    traditional = to_simplified_to_traditional(simplified)
    syllables = korean_syllables(sources["syl-2.txt"])

    for name, text in (("CN_SYMBOL", symbol), ("CN_CHARS", simplified)):
        non_bmp = [c for c in text if ord(c) > 0xFFFF]
        if non_bmp:
            raise SystemExit(f"{name} contains non-BMP characters: {non_bmp[:5]!r}")

    ranges_of = {
        "HIRAGANA_AND_KATAKANA": expand_ranges(cjk, "HIRAGANA_AND_KATAKANA"),
        "HANGUL_JAMO": expand_ranges(cjk, "HANGUL_JAMO"),
    }

    areas = []
    for name in area_order(lib):
        entry = {
            "name": name,
            "tag": TAGS.get(name, slugify(name)),
        }
        if name == "ZH_SC":
            entry |= {"order": "frequency", "chars": "".join(unique(list(simplified)))}
        elif name == "ZH_TC":
            entry |= {"order": "frequency", "chars": "".join(traditional)}
        elif name == "HANGUL_SYL":
            entry |= {"order": "frequency", "chars": "".join(unique(syllables))}
        else:
            pairs = ranges_of.get(name) or expand_ranges(lib, name)
            entry |= {"order": "codepoint", "ranges": [list(pair) for pair in pairs]}
        areas.append(entry)

    def area(name: str) -> dict:
        return next(item for item in areas if item["name"] == name)

    def range_size(entry: dict) -> int:
        return sum(stop - start + 1 for start, stop in entry["ranges"])

    checks = {
        "LATIN": range_size(area("LATIN")),
        "HIRAGANA_AND_KATAKANA": range_size(area("HIRAGANA_AND_KATAKANA")),
        "ZH_SYMBOL": range_size(area("ZH_SYMBOL")),
        "ZH_SC": len(area("ZH_SC")["chars"]),
        "ZH_TC": len(area("ZH_TC")["chars"]),
        "HANGUL_SYL": len(area("HANGUL_SYL")["chars"]),
    }
    # Ranges rather than exact counts: the upstream literal is the source of truth and
    # evolves (its own unit test still claims ZH_SC == 6763 while the shipped string
    # holds 6767 unique characters). These bounds only catch a garbled download or a
    # change in the OpenCC dictionaries.
    expected = {
        "LATIN": (127, 127),
        "HIRAGANA_AND_KATAKANA": (192, 192),
        "ZH_SYMBOL": (40, 120),
        "ZH_SC": (6000, 7500),
        "ZH_TC": (2000, 2600),
        "HANGUL_SYL": (1900, 2100),
    }
    for name, (low, high) in expected.items():
        if not low <= checks[name] <= high:
            raise SystemExit(
                f"{name}: expected {low}..{high} characters, got {checks[name]}"
            )

    payload = {
        "schema": 1,
        "version": DATA_VERSION,
        "source": {
            "repo": REPO,
            "ref": args.ref,
            "license": "Apache-2.0",
            "files": SOURCES,
            "generator": "tools/gen_lang_data.py",
        },
        "areas": areas,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.out} ({args.out.stat().st_size} bytes)", file=sys.stderr)
    for name, count in checks.items():
        print(f"  {name:24} {count:6}", file=sys.stderr)
    print(
        f"  ZH_SC starts with {''.join(area('ZH_SC')['chars'][:12])}", file=sys.stderr
    )
    print(
        f"  ZH_TC starts with {''.join(area('ZH_TC')['chars'][:12])}", file=sys.stderr
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
