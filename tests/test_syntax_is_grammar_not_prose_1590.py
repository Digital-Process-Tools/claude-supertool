"""#1590 — a registry `syntax` field is invocation grammar, never prose.

Every consumer prints `syntax` **whole**: the `ops` roster, `registry`, `help`,
and the synonym suggestion #1524 added. So whatever is in the field is paid for
on a line whose budget #1222 fought for, in a place a reader is scanning for the
shape of a call.

`gh-prs` was 243 characters against a p90 of 66 across the 86 ops that declare
one — the max, by 78 characters over the next field — because #1405 appended a
sentence to a parameter list:

    ...,anyauthor]  --  the release gate is gh-prs:merged-since=TAG,state=merged
    (was gh-since-tag, deleted #1405)

**Why the other 85 need no equivalent move, which #1590 asks to be said out
loud:** they were never in violation. `  --  ` occurs in exactly 1 of 86 fields
and an issue reference in exactly the same 1. The convention is not being
introduced here, it is being written down and enforced — 85 fields already obey
it, and the one that did not was made to.

**What the sentence was there for, and why nothing has to move to keep it.**
#1405's pinning test asserts the substring `merged-since=TAG,state=merged` is in
`syntax`, deliberately, so a deleted op's replacement could not go missing. That
substring is an **invocation**, not prose — it is grammar, and it stays in the
field. What leaves is the narration around it (`the release gate is`, `was
gh-since-tag, deleted #1405`), which was never pinned by anything and is already
carried, word for word, in the first 200 characters of the op's `description`
where #1405's own test also pins it. So the guarantee does not move; only the
duplication goes.

**And the shape it lands in is not invented for it.** ` | ` already separates
alternate invocation forms in 4 other fields — `gh-check`, `bluesky_list`,
`devto_list`, `hashnode_list` — and in every one of them each form begins with
the op's own name. A convention inferred from four fields is a convention; the
`  --  ` split the #1524 reviewer proposed, inferred from one, was refused for
exactly that reason and this file does not resurrect it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_ROOT = Path(__file__).parent.parent

# The longest pure-grammar field in the tree is `gl-mrs` at 165 characters, a
# filter board of the same shape as `gh-prs`. 243 is what got #1590 filed. The
# bound sits between them with headroom: it is a budget on the roster's width,
# not a style rule, and a field that needs more than this is telling you the
# extra belongs in `description`.
MAX_SYNTAX = 200


def _syntaxes() -> list[tuple[str, str, str]]:
    """`(preset_file, op_name, syntax)` for every shipped op declaring one."""
    out: list[tuple[str, str, str]] = []
    for path in sorted((_ROOT / "presets").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for name, entry in (data.get("ops") or {}).items():
            syntax = (entry or {}).get("syntax")
            if syntax:
                out.append((path.name, name, syntax))
    return out


def test_the_corpus_is_the_whole_shipped_tree() -> None:
    """A guard whose population silently shrank to nothing passes forever."""
    rows = _syntaxes()
    assert len(rows) >= 80, len(rows)
    assert any(name == "gh-prs" for _, name, _ in rows)


def test_no_syntax_field_carries_an_issue_reference() -> None:
    """`#1405` is provenance. Provenance is what `description`, the changelog
    and the tracker are for; a reader scanning for the shape of a call cannot
    use it, and it is the clearest machine-checkable marker of narration."""
    pattern = re.compile(r"#[0-9]")
    bad = [(f, n, s) for f, n, s in _syntaxes() if pattern.search(s)]
    assert not bad, bad


def test_no_syntax_field_carries_a_prose_separator() -> None:
    """The `  --  ` the #1524 reviewer proposed splitting on. One field had it,
    which is why splitting on it was refused — and why removing it, rather than
    teaching four consumers to read it, is the repair."""
    bad = [(f, n, s) for f, n, s in _syntaxes() if " -- " in s]
    assert not bad, bad


def test_no_syntax_field_is_wider_than_the_roster_budget() -> None:
    bad = [(n, len(s)) for _, n, s in _syntaxes() if len(s) > MAX_SYNTAX]
    assert not bad, bad


def test_every_alternate_form_is_an_invocation() -> None:
    """` | ` means *another way to call this op*, in all 5 fields that use it.

    Asserted over the whole corpus rather than over `gh-prs`, because a
    convention that holds for one field is the thing #1590 refused.
    """
    for _f, name, syntax in _syntaxes():
        if " | " not in syntax:
            continue
        for form in syntax.split(" | "):
            assert form.startswith(name), (name, form)


def test_the_release_gate_invocation_is_still_in_the_syntax_line() -> None:
    """#1405's guarantee, restated where #1590 leaves it.

    The synonym render prints this field under `Did you mean: gh-prs`, and bare
    `gh-prs` is every open PR on the repo — a different board from the one the
    caller typed `gh-since-tag` to reach. The substring is the load-bearing
    part; it is an invocation, so it belongs in `syntax` and stays.
    """
    syntax = dict((n, s) for _f, n, s in _syntaxes())["gh-prs"]
    assert "merged-since=TAG,state=merged" in syntax
    assert syntax.split(" | ")[-1] == "gh-prs:merged-since=TAG,state=merged"


def test_the_prose_that_left_syntax_was_already_in_the_description() -> None:
    """Nothing had to be relocated, which is why this is not a text edit with a
    guarantee dangling off it: `description` already opened on the same fact,
    and #1405's own test pins it there too."""
    data = json.loads((_ROOT / "presets" / "github.json").read_text(
        encoding="utf-8"))
    desc = data["ops"]["gh-prs"]["description"]
    assert "merged-since=TAG,state=merged" in desc[:400]
    assert "gh-since-tag" in desc[:400]
    assert "#1405" in desc[:400]
