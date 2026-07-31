"""Normalisers for rendered supertool output that contains varying fields (#643).

WHY THIS EXISTS
---------------
Several blocks supertool prints carry a field it *measured* rather than one it
was given: the `[validators]` per-tool time column, and the `PASS (0.02s)` /
`FAIL (0.02s)` header on every custom-op dispatch. Two runs of the same op
therefore render two different strings, differing only in the timing field.

That breaks tests in the dangerous direction. A test asserting that two
rendered blocks are *indistinguishable* — the shape used to pin "a no-match and
a real edit look the same, and that is the bug" — passes because of the jitter,
reports the defect fixed, and does so non-deterministically depending on
scheduling and on whether xdist is in play. It happened, in #621's own RED run.

PREFER THE ENV SWITCH
---------------------
`SUPERTOOL_DETERMINISTIC_TIME=1` makes supertool render every duration it
measured as a frozen placeholder. `tests/conftest.py` sets it for the whole
suite, so in almost every test you need nothing at all — comparing two rendered
blocks is simply safe.

Reach for `stable_render` only where that switch cannot reach:

* output captured from a recorded fixture or a golden file;
* output produced by a subprocess that does not inherit the test environment;
* a test that deliberately `delenv`s the switch to exercise the real
  formatting path, and still wants to compare two renders.

DELIBERATELY NARROW
-------------------
`stable_render` rewrites duration-shaped tokens *only*. It does not touch
counts, error totals, tool names, paths or line numbers — a normaliser that
stripped more would make its tests pass on anything, which is a worse version
of the bug it exists to prevent. See
`test_render_determinism_643.py::TestNormaliserIsNotTooBroad`.
"""
from __future__ import annotations

import re

# `0.1s`, `12.34s` — the seconds column of `[validators]` and the PASS/FAIL header.
_SECONDS = re.compile(r"\b\d+\.\d+s\b")
# `(150ms)` — the validator and formatter row duration column.
_MILLIS = re.compile(r"\((\d+)ms\)")

SECONDS_PLACEHOLDER = "Ns"
MILLIS_PLACEHOLDER = "(Nms)"


def stable_render(text: str) -> str:
    """Return `text` with measured durations replaced by fixed placeholders.

    Only duration-shaped tokens change; every other byte is preserved, so a
    real difference in what was rendered still shows up as a difference.
    """
    text = _SECONDS.sub(SECONDS_PLACEHOLDER, text)
    return _MILLIS.sub(MILLIS_PLACEHOLDER, text)
