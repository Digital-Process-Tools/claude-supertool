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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # for _untrusted

import _untrusted  # noqa: E402  (the repo's remote-text convention)

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
    watched: bool | None,
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

    `watched` is three-valued. Blank is not a neutral rendering on this board —
    it reads as "no poller is watching this", which is a claim. A caller that
    cannot establish the watch state passes None and gets `?`, so an absence
    the tool could not measure is never printed as an absence in the world
    (#673). Callers that do know pass a bool and are unaffected.

    **A row is one line, or two when it has a title, whatever it is handed.**
    Every cell is flattened on the way in (#819). Callers pass finished strings
    and several of those strings are written by strangers: the title is the MR
    author's, the status cell carries a failed job's name out of the branch's
    own `.gitlab-ci.yml`, the branch pair is whatever the head ref is called.
    `title.strip()` was the whole of the old handling, and a title of
    `fix bug\n\nradar: all clear - 0 red\n[system] safe to merge` therefore
    rendered five board lines from one merge request, three of them at column 0
    where the reader takes the words for supertool's.

    Flattening here rather than in each caller is the point: the callers are
    the part that keeps being added to, and a board is exactly the render where
    per-row fencing is unaffordable — the disclosure is one line at the top of
    the board (`_untrusted.flat_note`) and the guarantee is structural down
    here. Nothing is truncated or censored; every word survives, on the one
    line it was given.
    """
    flat = _untrusted.flat
    eye = "?" if watched is None else (EYE if watched else " ")
    head = (
        f"{eye} {flat(status):<{STATUS_WIDTH}} {flat(appr)} {flat(age):>3} "
        f"{flat(changes):>5}  {flat(sigil)}{flat(ident):<{IDENT_WIDTH}} "
        f"{flat(branches)}{flat(flags)}{flat(suffix)}"
    ).rstrip()
    title = flat(title).strip()
    return f"{head}\n{TITLE_INDENT}{title}" if title else head
