"""Anchoring a diagnostic parser on the path it was actually invoked with,
tolerant of the spellings a real tool's own platform can introduce (#1934,
#1937).

`re.escape(file)` alone assumes the tool echoes that exact string back.
actionlint does not (#1934 -- it relativises against its own CWD; see
actionlint.py's own `_line_re`), and even a tool that DOES try to echo the
same path can still differ in ways that carry no security meaning: `\\` vs
`/`, and a drive letter's case (`C:` vs `c:`). Real ruby on Windows CI
(windows-latest) was observed doing exactly this -- `tests/test_ruby_check.py`
went red there because the anchor built from the literal argv path matched
nothing, and a real, located diagnostic silently lost its line and column
(#1937). That is strictly worse than the #1934 misreport it replaced: a wrong
line is still a line, and this was none.

`path_variants` widens the anchor to a small, FIXED set of spellings that are
provably the same path as `file` -- never a wildcard. A forged filename
cannot use this to impersonate a different path: every variant is still a
transform of `file` itself, so what changes is which SPELLINGS of the real
path are accepted, never which paths are. This is pure string manipulation
with no filesystem access and no `os`-specific calls, so it can be (and is,
in `tests/test_adapter_line_re_anchor_1934.py`) exercised against
Windows-shaped paths on any platform -- the defect this module fixes was
found on a Windows CI leg, but neither the fix nor its test needs Windows to
run.

What this does NOT attempt: an 8.3 short filename (`RUNNER~1`) has no
general, filesystem-free way to expand back to its long form, so a tool that
prints one is not covered here. That residual gap is a documented, narrower
one than the separator/case difference actually observed in CI, and every
adapter using this module already has a designed fallback for "found output,
could not anchor a location in it" -- an unlocated finding naming the raw
output, never a silent `count: 0` (see the `if not errors and output:`
branch in each adapter's `main()`). Refusing with a stated reason, not
guessing, is the chosen answer for whatever this module's variants do not
cover.
"""
from __future__ import annotations

import re


def path_variants(file: str) -> list[str]:
    """Every spelling of `file` a diagnostic parser should accept as this
    adapter's own invoked path, de-duplicated, `file` itself always first.

    Covers: separator direction (a path can be echoed with `/` swapped for
    `\\` or vice versa), and a leading drive letter's case, tried against
    every separator form already collected.
    """
    variants = [file]
    for v in (file.replace("\\", "/"), file.replace("/", "\\")):
        if v not in variants:
            variants.append(v)
    for v in list(variants):
        if len(v) >= 2 and v[1] == ":" and v[0].isalpha():
            flipped = (v[0].lower() if v[0].isupper() else v[0].upper()) + v[1:]
            if flipped not in variants:
                variants.append(flipped)
    return variants


def anchor(file: str, tail: str) -> re.Pattern[str]:
    """`^(?:variant1|variant2|...)` + `tail`, anchored on every accepted
    spelling of `file`. `tail` is the adapter's own regex for everything
    after the path, including its own trailing `$`."""
    alternatives = "|".join(re.escape(v) for v in path_variants(file))
    return re.compile(r"^(?:" + alternatives + r")" + tail)
