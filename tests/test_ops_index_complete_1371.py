"""1371 — every op the dispatcher accepts must be reachable from
`docs/operations/index.md`, and by the route a reader who does not already know
the name would take.

`registry` shipped in #1363 and never reached the page. Enumerating from the
product rather than from the issue found six more in the same state: `check`,
`diag`, `guard`, `hover`, `ops-compact`, `rename`. Seven, not one — which is
why this test enumerates from `_valid_op_names()` instead of from a list
somebody types.

Two assertions, because two different readers fail differently:

- the **full op table** is what a reader who knows the name greps for. A name
  absent there is `docs:0` in the maintainer skill's own coverage audit.
- the **Categories table** is the only route for a reader who does *not* know
  the name. An op listed only in the full table is findable by grep and not by
  reading, and this page exists to be read.
"""
from __future__ import annotations

import re
from pathlib import Path

import supertool

INDEX = Path(__file__).parent.parent / "docs" / "operations" / "index.md"

# Names carrying their own punctuation in the page (`ops:roster`) or rendered as
# a slash pair (`replace` / `replace_dry`) still appear as bare backticked
# tokens, so one pattern covers both.
_BACKTICKED = re.compile(r"`([a-z][a-z0-9_-]*)")


def _names(text: str) -> set:
    return set(_BACKTICKED.findall(text))


def _section(text: str, heading: str) -> str:
    """The body under one `## heading`, up to the next `## `."""
    start = text.index(heading) + len(heading)
    rest = text[start:]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


def _index_text() -> str:
    """Line endings normalised, because the assertions below split paragraphs.

    There is no `.gitattributes` here and the Windows CI runners check out with
    `core.autocrlf=true`, so this file arrives CRLF there. A split on the
    two-newline paragraph break then finds none — a CRLF blank line holds no
    two adjacent newlines — and the count assertion would fail on Windows only,
    for a reason that has nothing to do with what it tests.
    """
    return INDEX.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10))


def test_every_dispatchable_op_is_in_the_full_op_table() -> None:
    table = _section(_index_text(), "## Full op table")
    missing = sorted(set(supertool._valid_op_names()) - _names(table))
    assert not missing, (
        f"dispatchable but absent from the full op table in {INDEX.name}: "
        f"{missing} — help:OP documents them and the reference does not"
    )


def test_every_dispatchable_op_is_reachable_from_the_categories_table() -> None:
    categories = _section(_index_text(), "## Categories")
    missing = sorted(set(supertool._valid_op_names()) - _names(categories))
    assert not missing, (
        f"absent from the Categories table in {INDEX.name}: {missing} — a "
        f"reader who does not know the name has no route to these"
    )


def test_the_op_count_in_the_opening_line_is_the_real_one() -> None:
    """The page opened with '~40 ops' while 44 dispatched.

    A tilde is not a licence: prose nobody can falsify is how `registry` sat
    unlisted for a release. The number is here so adding an op reddens this
    test instead of quietly widening the gap.
    """
    first_para = _index_text().split("\n\n")[1]
    stated = re.search(r"\b(\d+)\b", first_para)
    assert stated, f"no op count in the opening line of {INDEX.name}: {first_para!r}"
    assert int(stated.group(1)) == len(supertool._valid_op_names())
