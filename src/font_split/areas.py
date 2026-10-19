"""The language-area table: which characters are common in which language."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError

DATA_PATH = Path(__file__).with_name("data") / "lang-unicodes.json"
CJK_EXT_AREA = "CJK_EXT"
MISC_AREA = "MISC"

#: CJK ideograph blocks. Characters from these blocks that the frequency table does
#: not already claim are collected into the `cjk-ext` bucket instead of `misc`.
CJK_EXT_BLOCKS: tuple[tuple[int, int], ...] = (
    (0x3400, 0x4DBF),  # Ext A
    (0x4E00, 0x9FFF),  # URO
    (0xF900, 0xFAFF),  # Compatibility Ideographs
    (0x20000, 0x2A6DF),  # Ext B
    (0x2A700, 0x2B73F),  # Ext C
    (0x2B740, 0x2B81F),  # Ext D
    (0x2B820, 0x2CEAF),  # Ext E
    (0x2CEB0, 0x2EBEF),  # Ext F
    (0x2EBF0, 0x2EE5F),  # Ext I
    (0x2F800, 0x2FA1F),  # Compatibility Ideographs Supplement
    (0x30000, 0x3134F),  # Ext G
    (0x31350, 0x323AF),  # Ext H
)


def is_cjk_ext(code: int) -> bool:
    return any(start <= code <= stop for start, stop in CJK_EXT_BLOCKS)


@dataclass(frozen=True, slots=True)
class Area:
    """One entry of the area table, in table order."""

    name: str
    tag: str
    codepoints: tuple[int, ...]
    order: str  # "frequency" | "codepoint"


@dataclass(frozen=True, slots=True)
class AreaTable:
    version: str
    source: dict
    areas: tuple[Area, ...]


def load_area_table(path: Path | None = None) -> AreaTable:
    """Load the vendored table, or a user supplied replacement."""
    target = path or DATA_PATH
    if not target.is_file():
        raise ConfigError(f"area table not found: {target}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{target}: invalid JSON: {exc}") from exc

    raw_areas = payload.get("areas")
    if not isinstance(raw_areas, list) or not raw_areas:
        raise ConfigError(f"{target}: 'areas' must be a non-empty list")

    areas: list[Area] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_areas):
        where = f"{target}: areas[{index}]"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where} must be an object")
        name = raw.get("name")
        tag = raw.get("tag")
        order = raw.get("order", "codepoint")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{where}: 'name' is required")
        if name in seen:
            raise ConfigError(f"{where}: duplicate area name {name!r}")
        seen.add(name)
        if not isinstance(tag, str) or not tag:
            raise ConfigError(f"{where}: 'tag' is required")
        if order not in {"codepoint", "frequency"}:
            raise ConfigError(f"{where}: 'order' must be 'codepoint' or 'frequency'")

        if "ranges" in raw:
            pairs = raw["ranges"]
            if not isinstance(pairs, list) or not pairs:
                raise ConfigError(f"{where}: 'ranges' must be a non-empty list")
            expanded: list[int] = []
            for pair in pairs:
                if not (isinstance(pair, list) and len(pair) == 2):
                    raise ConfigError(f"{where}: each range must be [start, end]")
                start, stop = pair
                if not all(isinstance(v, int) for v in (start, stop)) or stop < start:
                    raise ConfigError(f"{where}: malformed range {pair!r}")
                expanded.extend(range(start, stop + 1))
            # The upstream table lists U+2014 twice; a codepoint must be claimed once.
            codepoints = list(dict.fromkeys(expanded))
        else:
            chars = raw.get("chars")
            if not isinstance(chars, str) or not chars:
                raise ConfigError(f"{where}: either 'ranges' or 'chars' is required")
            codepoints = list(dict.fromkeys(ord(char) for char in chars))

        areas.append(
            Area(name=name, tag=tag, codepoints=tuple(codepoints), order=order)
        )

    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    return AreaTable(
        version=str(payload.get("version", "unknown")),
        source=source,
        areas=tuple(areas),
    )
