#!/usr/bin/env python3


















from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  

import _untrusted  

EYE = "👁"
TITLE_INDENT = " " * 8
STATUS_WIDTH = 16
IDENT_WIDTH = 6


def branch_pair(source: object, target: object) -> str:





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





























    flat = _untrusted.flat
    eye = "?" if watched is None else (EYE if watched else " ")
    head = (
        f"{eye} {flat(status):<{STATUS_WIDTH}} {flat(appr)} {flat(age):>3} "
        f"{flat(changes):>5}  {flat(sigil)}{flat(ident):<{IDENT_WIDTH}} "
        f"{flat(branches)}{flat(flags)}{flat(suffix)}"
    ).rstrip()
    title = flat(title).strip()
    return f"{head}\n{TITLE_INDENT}{title}" if title else head
