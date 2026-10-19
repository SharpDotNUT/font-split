"""Small formatting helpers used by the CLI."""

from __future__ import annotations


def human_bytes(count: float) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(count)
    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"
