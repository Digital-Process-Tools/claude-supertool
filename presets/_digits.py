"""The one "is this string a number" test the presets share (#1727).

`str.isdigit()` is not it, and the reason is two distinct classes of character
rather than one:

* **Unicode decimals** — `٢` (U+0662 ARABIC-INDIC DIGIT TWO), `۲`, `২`.
  `isdigit()` and `isdecimal()` are both True, so `int()` converts them
  happily. An op that accepted one proceeded against a number the caller never
  typed, and rendered that number back in a receipt the caller cannot match to
  what they wrote.
* **Superscripts** — `²` (U+00B2), `³`, `¹`. `isdigit()` is True and
  `isdecimal()` is False, so `int()` **raises**. Every `isdigit()`-then-`int()`
  site was therefore an uncaught `ValueError` on caller-supplied text, and four
  ops under `presets/github/` plus the shared filter-value guard reached it from
  a plain op string.

The test lived in three places before this module and was spelled two ways —
`_job_argv._DIGITS` and `run._DIGITS` (correct, each carrying its own copy of
the rationale), `pr_create` and eight others (`str.isdigit()`). #1727 was filed
on the pairing itself rather than on any one line: a guard that reads as
deliberate in one file and accidental in its neighbour gets copied from the
neighbour.

`isdecimal()` would close the crash and not the first class, so it is not the
answer either. What every caller here actually means is **ASCII digits**, which
is what a forge id, a page size and a PR number are.
"""
from __future__ import annotations

import re

#: Anchored with a capital-Z escape and not `$`: Python's `$` also matches
#: immediately before a final newline, so `^[0-9]+$` accepts a value with one
#: appended (#1188). Exported because two callers walk a value character by
#: character to name which characters were strays, and a second regex for that
#: is a second thing to keep in step.
DIGITS = re.compile(r"^[0-9]+\Z")


def is_ascii_int(text: str) -> bool:
    """True when `text` is one or more ASCII digits and nothing else.

    No stripping, no sign, no separators. A caller that wants to allow leading
    whitespace strips it itself, so the allowance is visible at the call site
    rather than hidden in here.
    """
    return bool(DIGITS.match(text))
