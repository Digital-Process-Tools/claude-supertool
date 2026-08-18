"""#1774 — `description` is rendered whole by `ops`, so its length is a per-session tax.

`ops` and `help:OP` print the **same** `description` field, byte for byte: there
is no long form today. Measured on 0.46.0, `gh-prs` carries 4,512 characters of
`description`, its `ops` row is 4,706 bytes and its `help:gh-prs` render is
4,721 — the difference is the syntax line and the payload-route footer, nothing
else. So a clause added "for the maintainer record" is not filed somewhere
cheaper; it is injected into every reader of the roster, forever.

What that costs, over the whole shipped tree (128 documented ops):

    total 76,795   median 151   p90 1,868   max 6,578 (`channel`)
    top 10 rows    37,739 = 49% of the corpus, in 8% of the ops

`_HOOK_OUTPUT_CAP_BYTES` is 7,168 and the assembled `ops` render is 74,838 —
over the SessionStart cap by 10x, which is why this repo's own session
injection has fallen back to a bare list of op names carrying no signatures.

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
    "channel": 6578,
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
    "radar": 2141,
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
        f"{grew}. #1774 — this field is paid for by every reader of `ops`."
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
