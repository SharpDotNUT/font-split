"""`@font-face` generation.

One face per shard, in ascending shard order (which is also the loading priority),
with a `unicode-range` built from that shard's real character set — so ranges never
overlap and no character is left without an owner.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

DEFAULT_DISPLAY = "swap"


@dataclass(frozen=True, slots=True)
class Face:
    file: str
    unicode_range: str


def render_css(
    *,
    family: str,
    faces: Sequence[Face],
    weight: str,
    style: str,
    display: str = DEFAULT_DISPLAY,
    header: str | None = None,
) -> str:
    blocks = []
    for face in faces:
        local = f'local("{_escape(family)}")'
        url = f'url("./{face.file}") format("woff2")'
        blocks.append(
            "@font-face {\n"
            f'  font-family: "{_escape(family)}";\n'
            f"  src: {local}, {url};\n"
            f"  font-style: {style};\n"
            f"  font-weight: {weight};\n"
            f"  font-display: {display};\n"
            f"  unicode-range: {face.unicode_range};\n"
            "}\n"
        )
    parts = []
    if header:
        parts.append(f"/* {header} */\n")
    parts.extend(blocks)
    return "\n".join(parts)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _quote(value: str) -> str:
    return f'"{_escape(value)}"'
