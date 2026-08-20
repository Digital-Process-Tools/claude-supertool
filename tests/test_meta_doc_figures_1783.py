"""#1783 items 4-6 — three stale claims in `docs/operations/meta.md`, one species.

All three are a re-measurement that did not reach the whole file: a quoted
transcript left behind when the string it quotes changed, two sizes for
`ops:roster` four lines apart, and a sentence describing #1781's own correction
that repeats the error it corrected.

Prose is not gated by anything, which is why it rots. These four tests are the
gate: each pins a documented claim to the thing it claims about, so the next
person who changes the render finds out from CI instead of from an audit.

**Why the pinnable numbers are the differences, not the renders.** `ops`,
`ops:full`, `ops-compact` and `ops:roster` all carry `_preset_disclosure()`,
which names the *absolute path* of the config it read — so every one of those
renders is a function of where the checkout sits on disk.
`tests/test_ops_roster_1231.py` already states that as policy for the roster
("deliberately *not* pinned to a literal in prose"). Measured 2026-08-20, the
same tree at two paths:

    /Users/…/claude-supertool  (46 chars)   roster 1969  ops 3727  full 72715
    /Users/…/st-wt/1783        (40 chars)   roster 1963  ops 3721  full 72709

Exactly the six-byte path difference, in every render. That is also the whole
of the discrepancy between this file and #1783's own figures for three of the
four — the issue measured in the clone and was right there.

So a KB approximation is safe (six bytes cannot move 1.9KB) and an exact byte
count is not. One exact number *is* safe: `ops:full` minus `ops` cancels the
disclosure, which each carries once, and comes to 68,988 at both paths. That is
the descriptions' real cost, and it is the number item 6 is about.

Separate file rather than an extension of `test_ops_roster_1231.py`'s stale
figure sweep: that file was held by another lane when this was written.
"""
from __future__ import annotations

import re
from pathlib import Path

import supertool

REPO_ROOT = Path(__file__).parent.parent
META = REPO_ROOT / "docs" / "operations" / "meta.md"


def _meta() -> str:
    return META.read_text(encoding="utf-8")


def test_the_quoted_refusal_transcript_is_what_the_op_actually_prints(
        shipped_config) -> None:
    """Item 4. The fence quotes `ops:gh-labels`; run it and compare.

    A transcript nothing re-runs is a transcript that rots — this one kept its
    last line through the change that rewrote it. #1783 guessed at the
    replacement and guessed wrong, which is the argument for executing the
    command rather than editing the fence to what it ought to say.
    """
    text = _meta()
    marker = "$ ./supertool 'ops:gh-labels'"
    assert marker in text, "the transcript for ops:gh-labels is gone from meta.md"
    after = text.split(marker, 1)[1]
    quoted = after.split("```", 1)[0].strip("\n")
    actual = supertool.dispatch("ops:gh-labels").rstrip("\n")
    assert quoted == actual, (
        "meta.md quotes a transcript the op no longer prints.\n"
        "--- quoted ---\n" + quoted + "\n--- actual ---\n" + actual)


def test_the_roster_size_is_stated_once(shipped_config) -> None:
    """Item 5. Two sizes four lines apart, and both were stale.

    Path-independent: it compares the file's claims with each other, so it
    fails for the reason it is named after and not for where CI checked out.
    """
    claims = set(re.findall(r"`ops:roster`[^.\n]*?~([\d.]+)KB", _meta()))
    assert len(claims) <= 1, f"meta.md states {sorted(claims)} for ops:roster"


def test_the_roster_size_is_this_checkouts(shipped_config) -> None:
    """Item 5, other half. Agreeing with itself is not the same as being right.

    A tenth of a KB is 100 bytes, more than an order above the six-byte
    path-length variance measured in the docstring, so this cannot red for the
    checkout directory.

    Decimal KB, because that is the convention the file already uses: it states
    `ops` as ~3.7KB and `ops` renders 3,727 bytes. Dividing by 1024 would give
    3.64 and put every existing figure in the file slightly out.
    """
    claims = set(re.findall(r"`ops:roster`[^.\n]*?~([\d.]+)KB", _meta()))
    assert claims, "meta.md no longer states a size for ops:roster"
    stated = float(claims.pop())
    actual = len(supertool.op_ops_roster().encode("utf-8")) / 1000
    assert abs(stated - actual) < 0.1, (
        f"meta.md says ~{stated}KB, this checkout renders {actual:.2f}KB")


def test_the_footer_reports_the_render_and_the_doc_quotes_the_difference(
        shipped_config) -> None:
    """Item 6. `ops`' footer says what `ops:full` costs *in total*.

    meta.md said it reported "how many bytes the descriptions cost", which is
    the total minus `ops` itself. Both halves are asserted: the footer really
    does carry the whole-render number and not the difference, and the doc
    really does quote the difference. Either one alone would let the sentence
    be wrong in the other direction.
    """
    body = supertool.op_ops()
    full = supertool.op_ops(full=True)
    whole = len(full.encode("utf-8"))
    descriptions = whole - len(body.encode("utf-8"))

    assert str(whole) in body, "the footer stopped reporting the whole render"
    assert str(descriptions) not in body, (
        "the footer now reports the difference — meta.md needs rewording, "
        "not this assertion relaxed")
    assert f"{descriptions:,}" in _meta(), (
        f"meta.md does not quote the descriptions' cost ({descriptions:,})")
