#!/usr/bin/env python3
"""Triage-board row layout, shared by every board a human reads.

`gl-mrs`, `radar` and `gh-prs` render different objects with different
vocabularies (iid vs number, pipeline vs checks, source_branch vs
headRefName), but they are one board to the reader: same columns, same
widths, same two-line shape. That layout is the only thing they share, and
it is exactly what this module owns.

The contract is deliberately stringly-typed: callers compute their own cells
in their own vocabulary and hand over finished strings. No provider field
name reaches this file, so unifying the layout costs no shared field model.

Line 1 is the status line and ends with the branch pair; line 2 carries the
full title. One line cannot hold both, and the old single line resolved that
by truncating the title — which cut rows mid-word, exactly where the
disambiguating detail lives (#421, #424). Branch wins the status line
because branch is actionable and title is context.
"""
from __future__ import annotations

EYE = "👁"
TITLE_INDENT = " " * 8
STATUS_WIDTH = 16
IDENT_WIDTH = 6


def branch_pair(source: object, target: object) -> str:
    """`source -> target`, the field a human acts on (checkout, worktree add).

    Empty when neither side is known — a bare '? -> ?' would be noise, not
    information.
    """
    src = str(source or "")
    tgt = str(target or "")
    if not src and not tgt:
        return ""
    return f"{src or '?'} -> {tgt or '?'}"


def render_row(
    *,
    sigil: str,
    ident: str,
    watched: bool,
    status: str,
    appr: str,
    age: str,
    changes: str,
    branches: str,
    flags: str = "",
    title: str = "",
    suffix: str = "",
) -> str:
    """One triage row, two lines — the single definition of the board format.

    `suffix` is appended to the status line so callers that annotate rows
    (radar's drift/healed marks) land their marks there rather than on the
    title line.
    """
    eye = EYE if watched else " "
    head = (
        f"{eye} {status:<{STATUS_WIDTH}} {appr} {age:>3} {changes:>5}  "
        f"{sigil}{ident:<{IDENT_WIDTH}} {branches}{flags}{suffix}"
    ).rstrip()
    title = title.strip()
    return f"{head}\n{TITLE_INDENT}{title}" if title else head
