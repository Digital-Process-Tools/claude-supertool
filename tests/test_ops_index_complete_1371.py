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

ROOT = Path(__file__).parent.parent
INDEX = ROOT / "docs" / "operations" / "index.md"

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


def _dispatchable_names(monkeypatch) -> set:
    """Every name this checkout would accept: builtins plus config/preset ops.

    `_valid_op_names()` alone is the wrong denominator for the honesty check —
    the page legitimately carries `git-blame`, which ships with the `git`
    preset. conftest's `_disable_rtk_and_config` pins `_CONFIG = {}`, so the
    config has to be loaded back for the duration of this one assertion.
    """
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    config = supertool._load_config()
    names = set(supertool._valid_op_names())
    for section in ("builtin-ops", "ops", "aliases"):
        names |= set(config.get(section, {}))
    assert len(names) > len(supertool._valid_op_names()), (
        "no config op names loaded — the check below would be against builtins "
        "only and would flag every preset op on the page")
    return names


def test_neither_table_names_an_op_that_does_not_dispatch(monkeypatch) -> None:
    """The other direction, and the reference had one (#1371 review).

    Completeness and honesty are separate properties: the two assertions above
    are satisfied by a page that also lists ops which do not exist. The page
    listed `blame`, syntax cell and all, and `blame:PATH:LINE` answers
    `unknown operation: blame` — the op is `git-blame`, from the git preset,
    documented in `docs/presets/git.md`. A reader following this reference
    into a refusal is the same defect as one who never found the op, one
    surface along.

    Scoped to the builtin reference: this page enumerates what the dispatcher
    accepts, and a preset op belongs in `docs/presets/`. Names are read as the
    head of a `:`-form, so `ops:roster` checks as `ops`.
    """
    text = _index_text()
    named = set()
    for section in ("## Categories", "## Full op table"):
        for line in _section(text, section).splitlines():
            if not line.startswith("|") or line.startswith("|--"):
                continue
            cells = line.split("|")
            # Categories puts the names in cell 2 (cell 1 is the category
            # label); the full op table puts the op in cell 1.
            for cell in cells[1:3]:
                named.update(n.split(":")[0] for n in _BACKTICKED.findall(cell))
    phantom = sorted(named - _dispatchable_names(monkeypatch))
    assert not phantom, (
        f"{INDEX.name} names ops the dispatcher does not accept: {phantom} — "
        f"each one sends a reader to `unknown operation`"
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
