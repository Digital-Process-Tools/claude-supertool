"""#1774 — `description` is rendered whole by `ops:full` and `help:OP`, so its length is paid for by both on demand.

#1813: this was wrong as of #1775/#1778, which moved `description` out of the
default `ops` listing. Corrected here rather than left to drift a second time.

`ops` (bare, no argument) has been **signatures only** since #1774/#1775/#1778
— measured here at 3,721 bytes. `description` is now paid for by two
on-demand surfaces instead, and both still render it whole: `ops:full` prints
every row's description alongside its signature, and `help:OP` prints one
op's description verbatim — `op_help()` has no `compact`/`full` distinction of
its own and never developed one, so shrinking a description shrinks `help:OP`
by exactly as much. **The obvious remedy — "move the prose into `help:OP`" —
does not exist**; the cheaper home is the docs page for that op's family (see
`docs/contributing.md`'s own `description` section), not a surface that
already carries the same bytes.

**The ratchet is unaffected by any of this.** A field two on-demand surfaces
still render whole is worth bounding regardless of who pays for it by default.
What changed is only the entry point a maintainer reads when the test goes
red — it names `ops:full` and `help:OP`, not `ops`.

What the corpus costs, over the whole shipped tree (128 documented ops):

    total 74,679   median 152   p90 1,732   max 6,578 (`channel`)
    top 10 rows    35,290 = 47% of the corpus, in 8% of the ops

`_HOOK_OUTPUT_CAP_BYTES` is 7,168 and `ops:full` renders far past it (tens of
KB, and checkout-path-dependent to the byte — `tests/test_render_size_claims_1877.py`
is where an exact figure is pinned, not here). That gap is why `ops:full` is
never what a SessionStart-style injection sends; `ops:roster` (names plus
safety class, no descriptions at all) is what `hooks/session-start.sh` sends
instead, and bare `ops` — signatures, no descriptions — fits the cap on its
own without needing a fallback.

This file is a **ratchet, not a style rule.** Every entry in `_OVER_BUDGET` is a
description that is already over `MAX_DESCRIPTION`; each may shrink and none may
grow, and an op absent from the ledger must come in under the cap. Nothing here
asks for a rewrite in one PR — it makes the direction one-way while #1774
decides the shape.

The cap is p90 rounded up, deliberately: nine ops in ten already fit under it,
so it is the corpus's own habit written down rather than a budget invented for
the twelve that broke it.
"""
from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).parent.parent

# p90 of the corpus, rounded up. A new op needing more than this is telling you
# the extra is provenance, and provenance has homes that are not paid for on
# every `ops` call: the issue, the changelog fragment, the test's own docstring.
MAX_DESCRIPTION = 2000

# The twelve over budget at 0.46.0, with their exact size. Sizes may only fall.
# An op that drops under MAX_DESCRIPTION comes **out** of this ledger — that is
# how the burn-down stays visible rather than sitting here at a stale number.
_OVER_BUDGET = {
    "channel": 6492,
    "gh-prs": 4512,
    "git-push": 3991,
    "gh-pr-merge": 3589,
    "git-commit": 3463,
    "gh-issues": 2866,
    "gh-branch": 2784,
    "gh-labels": 2723,
    "dashboard": 2401,
    "git-worktrees": 2383,
    "guard": 2166,
    "radar": 2138,
}


def _descriptions() -> list[tuple[str, str, int]]:
    """`(source_file, op_name, length)` for every documented op in the tree.

    Both halves of the roster, because both are rendered by the same `ops` call:
    the shipped presets and this repo's own `builtin-ops` block.
    """
    out: list[tuple[str, str, int]] = []
    for path in sorted((_ROOT / "presets").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for name, entry in (data.get("ops") or {}).items():
            desc = (entry or {}).get("description")
            if desc:
                out.append((path.name, name, len(desc)))
    root = json.loads((_ROOT / ".supertool.json").read_text(encoding="utf-8"))
    for name, entry in (root.get("builtin-ops") or {}).items():
        if isinstance(entry, dict) and entry.get("description"):
            out.append((".supertool.json", name, len(entry["description"])))
    return out


def test_the_corpus_is_the_whole_documented_tree() -> None:
    """A ratchet whose population silently shrank to nothing passes forever."""
    rows = _descriptions()
    assert len(rows) >= 120, len(rows)
    names = {name for _f, name, _n in rows}
    assert "gh-prs" in names and "read" in names


def test_no_new_op_is_over_the_roster_budget() -> None:
    """An op absent from the ledger fits, or the ledger is the wrong answer.

    If you are here because a new op needs more room: it does not. Split the
    provenance out. If you are here because an *existing* op grew past the cap,
    that op is one of the twelve and the failure below is the other test.
    """
    bad = [
        (name, size)
        for _f, name, size in _descriptions()
        if name not in _OVER_BUDGET and size > MAX_DESCRIPTION
    ]
    assert not bad, (
        f"description over {MAX_DESCRIPTION} chars and not in the #1774 "
        f"ledger: {bad}. The roster prints this field whole on every call."
    )


def test_the_over_budget_ledger_only_ever_falls() -> None:
    """The ratchet itself. Grow one of the twelve and this is what says so."""
    sizes = {name: size for _f, name, size in _descriptions()}
    grew = [
        (name, recorded, sizes[name])
        for name, recorded in _OVER_BUDGET.items()
        if name in sizes and sizes[name] > recorded
    ]
    assert not grew, (
        f"description grew on an op already over budget (name, was, now): "
        f"{grew}. #1774 — this field is paid for by every reader of `ops:full` "
        f"and `help:OP` (#1813)."
    )


def test_a_shrunk_entry_leaves_the_ledger_rather_than_going_stale() -> None:
    """Two ways the ledger lies if nothing checks it.

    An entry recorded above its real size quietly buys headroom back, and an
    entry that has come under the cap keeps claiming a debt that is paid. Both
    read as progress and neither is; the remedy in each case is one line.
    """
    sizes = {name: size for _f, name, size in _descriptions()}
    stale = [
        (name, recorded, sizes.get(name))
        for name, recorded in _OVER_BUDGET.items()
        if name not in sizes or sizes[name] != recorded
    ]
    assert not stale, (
        f"#1774 ledger out of date (name, recorded, actual): {stale}. "
        f"Update the number, or delete the row if the op is now under "
        f"{MAX_DESCRIPTION}."
    )
