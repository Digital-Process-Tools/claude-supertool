



























from __future__ import annotations

import pathlib

from linebreaks import split_lines


def context_fields(file_path: str, line: int | None, radius: int = 2) -> dict:










    if line is None:
        return {}
    try:
        text = pathlib.Path(file_path).read_text(errors="replace", encoding="utf-8")
    except OSError as exc:



        detail = exc.strerror or str(exc)
        reason = f"{type(exc).__name__} reading {file_path}: {detail}"
        return {"source_context": [],
                "context_unavailable": " ".join(reason.split())}
    lines = split_lines(text)
    ctx: list[str] = []
    for offset in range(-radius, radius + 1):
        ln = line + offset
        if 1 <= ln <= len(lines):
            prefix = f"{ln}→" if offset == 0 else f"{ln}:"
            ctx.append(f"{prefix} {lines[ln - 1]}")
    return {"source_context": ctx}
