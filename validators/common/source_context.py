"""Shared source-context helper for validator adapters.

`source_context()` used to return `list[str]` and swallow `OSError` into `[]` —
the same value it returns for a located finding whose window falls outside the
file (#1446). Two different facts, one shape, and every receipt rendered them
identically: this repo's house defect, an absence produced by the tool read as
an absence in the world.

So the helper hands back **fields**, not a list, and the failure gets its own
key. Three states, not two:

    line is None            -> {}                          nothing was located
    read ok                 -> {"source_context": [...]}   lines, possibly []
    read failed             -> {"source_context": [],
                                "context_unavailable": reason}

The asymmetry is deliberate and it is the one #1443 settled next door in
`pkg_paths`: **the finding survives.** The tool said something is wrong at that
line, and that claim does not depend on our ability to reprint the line. Only
the illustration is missing, so only the illustration is qualified. Routing this
to `skipped` would drop `errors` entirely and lose a true diagnostic to a failed
`open()`.

`source_context()` itself is gone rather than deprecated. Left in place, the
next adapter author copies the call that was already in twenty-eight files and
reintroduces the swallow in a file nobody is looking at — which is exactly how
`phpstan-mcp` and `phpunit-mcp` came to carry private copies of it.
"""
from __future__ import annotations

import pathlib

from linebreaks import split_lines


def context_fields(file_path: str, line: int | None, radius: int = 2) -> dict:
    """Error-object fields for the source lines around `line`.

    Spread into the error dict — `{..., **context_fields(target, ln)}` — or
    `err.update(context_fields(target, ln))`. Never assign the result to
    `source_context`: the point is that it carries a second key.

    Lines are rendered with the error line prefixed ``N→`` and its neighbours
    ``N:`` (radius=2 → lines line-2..line+2), per `validators/SCHEMA.md`
    §"Error object".
    """
    if line is None:
        return {}
    try:
        text = pathlib.Path(file_path).read_text(errors="replace", encoding="utf-8")
    except OSError as exc:
        # The reason goes on a line of its own in the verbose receipt, so it is
        # flattened here as well as there: `strerror` is the OS's own words and
        # a path can hold anything, newlines included.
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
