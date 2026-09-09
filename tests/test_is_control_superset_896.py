"""#896 F4 -- `_untrusted._is_control` is a strict superset of the code
points `str.splitlines()` breaks on, not an exact match in both directions.

The docstring above `_is_control` used to say the predicate covers "exactly,
in both directions" the set `str.splitlines()` splits on. A full sweep says
otherwise: every one of the ten `splitlines()` separators is covered (no
false negative, which is the direction that actually protects a reader), but
`_is_control` additionally covers 57 more code points -- the rest of C0, all
of C1, and DEL -- that `splitlines()` never breaks a line on. Fixed here as
a docstring correction only (#896 calls this cosmetic: the extra coverage is
the safe direction and no ordinary path is affected), pinned so the same
overclaim cannot drift back in.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "presets"))
import _untrusted  # noqa: E402


def _splitlines_breaks_on() -> set:
    """Every code point in 0x0-0x10FFFF that `str.splitlines()` treats as a
    line boundary, measured directly rather than transcribed."""
    breaks = set()
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue  # surrogates -- not valid str contents
        ch = chr(cp)
        if ("x" + ch + "y").splitlines() != ["x" + ch + "y"]:
            breaks.add(cp)
    return breaks


def test_is_control_never_misses_a_splitlines_boundary() -> None:
    """The direction that actually matters for a reader counting lines:
    `_is_control` must never be missing one of `str.splitlines()`'s own
    separators, or `flat()`/`visible()` would let a line through uncounted."""
    breaks = _splitlines_breaks_on()
    assert len(breaks) == 10
    missed = {cp for cp in breaks if not _untrusted._is_control(chr(cp))}
    assert missed == set(), (
        f"_is_control misses splitlines() boundaries: "
        f"{sorted(hex(cp) for cp in missed)}")


def test_is_control_is_a_strict_superset_by_57() -> None:
    """The docstring's corrected claim: `_is_control` also fires on 57 code
    points `str.splitlines()` does not treat as a boundary at all (the rest
    of C0, all of C1, DEL) -- a superset, not an exact match."""
    breaks = _splitlines_breaks_on()
    is_control_true = {cp for cp in range(0x110000)
                        if not (0xD800 <= cp <= 0xDFFF)
                        and _untrusted._is_control(chr(cp))}
    extra = is_control_true - breaks
    assert len(extra) == 57
    assert breaks - is_control_true == set(), (
        "a splitlines() boundary _is_control does not cover would be the "
        "unsafe direction (a forged line _is_control cannot see)")
