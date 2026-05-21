"""Shared source-context helper for validator adapters."""
from __future__ import annotations

import pathlib


def source_context(file_path: str, line: int | None, radius: int = 2) -> list[str]:
    """Return 5 lines centered on `line` (radius=2 → lines line-2..line+2).

    Error line prefixed ``N→``, others prefixed ``N:``. Returns [] if line is
    None or file unreadable.
    """
    if line is None:
        return []
    try:
        lines = pathlib.Path(file_path).read_text(errors="replace").splitlines()
    except OSError:
        return []
    ctx: list[str] = []
    for offset in range(-radius, radius + 1):
        ln = line + offset
        if 1 <= ln <= len(lines):
            prefix = f"{ln}→" if offset == 0 else f"{ln}:"
            ctx.append(f"{prefix} {lines[ln - 1]}")
    return ctx
