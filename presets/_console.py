"""Make a preset's own glyphs survive the console it was started on (#1388).

Every preset prints characters this repo chose — an em dash in a hint line, a
`✓` on a receipt, the `→` in `gh-starred`. On a Windows console those are not a
rendering problem, they are a fatal one: cp437 (the US default), cp850 and
cp1252 each map only part of that repertoire, and a `print()` of a character
the codec has no mapping for raises `UnicodeEncodeError` mid-render. The
process dies partway through its own output, so the work lands and the receipt
says it crashed — which invites the operator to run it again (#308, #415).

**This is only about the path with no supertool in front of it.** `_supertool.py`
sets `PYTHONIOENCODING=utf-8` in `_main` before any op dispatches, so a preset
spawned as an op already has a UTF-8 stdout whatever the console codepage is.
What that does not reach is a script a human runs straight from a shell —
`python3 presets/github/starred.py 5` — which is exactly the case
`presets/git/` was given this treatment for, and the case #1388 was filed
about. `tests/test_encoding_seam.py` enumerates both halves.

**Reconfiguring, not detecting, and the trade is real.** After this call
`_untrusted._stream()` reads ``utf-8`` and prints its guillemet markers rather
than the ASCII fallback #863 added for exactly this console — and on a
genuinely cp437 console UTF-8 bytes are mojibake, which `_untrusted`'s docstring
argues against at length. Two things settle it here. The supertool path already
behaves this way, because `PYTHONIOENCODING=utf-8` reaches `_stream()` by the
same road; and the alternative on the direct path is not a degraded marker but
a process that prints a correctly-degraded banner and then dies four lines
later, which is worth nothing to the reader the banner exists for.

**Why this is a second copy.** `presets/git/_git_common.py` has the identical
function, and #555 is this repo's standing objection to exactly that. It stays
duplicated for one release because that file is held by another branch as this
is written; folding the git copy into this module is filed rather than done.
The rule in `tests/test_encoding_seam.py` keys on the *called name*, so both
copies satisfy it and a preset importing either is judged the same way.
"""
from __future__ import annotations

import sys


def use_utf8_stdout() -> None:
    """Force UTF-8 on this process's stdout and stderr.

    A stream without ``reconfigure`` — wrapped or replaced, as under pytest's
    capture — is left alone rather than treated as an error, and a codec the
    platform will not accept is swallowed for the same reason: this is a
    precondition for printing, not the thing being printed, and it must never
    become the reason an op fails.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass
