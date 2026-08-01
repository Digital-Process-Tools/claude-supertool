"""One body-cap cut, and one way of saying it happened (#681, #698).

Four read ops render a long body under a cap: `gh-issue`, `gl-issue`, `gh-pr`,
`gl-mr`. All four used the same raw `body[:MAX]` — no marker, no count. #681
fixed one of them; #698 found the other three still doing it and had to choose
between copying that fix three more times or naming it once. Copied, the sixth
site drifts: the whole defect class here is a disclosure that exists at some
call sites and not others, and four hand-maintained copies of a disclosure is
how you get a fifth that forgets.

So the shape lives here, and the two already-correct sites were moved onto it
rather than left as a second copy of it. That refactor is safe to make inside a
bugfix because #681 pinned `gh-issue`'s exact output in four tests — if this
module renders one character differently, those go red.

Two decisions, both load-bearing:

**The cut lands on a line break, not a byte offset.** `body[:cap]` ends
wherever the count runs out, which in #681's own repro was three characters
into a heading — printed as `## The`, with the next section starting right
after, so the truncated render read as a complete one. A marker cannot repair
that: malformed markdown plus a natural-looking ending is a *different*
document, not a shorter one. Backing off to the last `\\n` at-or-before the cap
means the body always ends where a line ended.

**The disclosure is stated twice, and the escape hatch has to be real.** Once
in the header, before the reader reaches the description — a footer-only notice
is read by nobody in exactly the case it exists for, since the reader being cut
off is cut off before reaching it — and once at the point of the cut. Both name
the exact character count withheld and both say `:full`. That last part is why
#698 also had to give `gh-pr` a `:full` flag (it had none) and make `gl-mr`'s
`:full` actually uncap the description (it did not). A disclosure pointing at a
flag that does not work is worse than no disclosure: it reads as a way out.
"""
from __future__ import annotations


def cut(body: str, cap: int | None) -> tuple[str, int]:
    """Return ``(shown, withheld)`` for ``body`` under ``cap``.

    ``cap`` of ``None`` (the ``:full`` path) returns the body untouched.
    ``withheld == 0`` means nothing was cut and no disclosure is owed —
    callers branch on it rather than re-deriving the comparison.
    """
    if cap is None or cap <= 0 or len(body) <= cap:
        return body, 0
    cut_at = body.rfind("\n", 0, cap)
    if cut_at <= 0:
        # No line break to back off to — a single long paragraph. The byte
        # offset is all there is, but it is now disclosed, which is the part
        # that was missing.
        cut_at = cap
    shown = body[:cut_at]
    return shown, len(body) - len(shown)


def header_notice(shown: str, total: int, withheld: int) -> str:
    """The header line, printed before the body the reader may never reach."""
    return (
        f"Body: TRUNCATED — {len(shown)} of {total} chars shown, "
        f"{withheld} withheld — use :full to fetch all"
    )


def cut_notice(withheld: int) -> str:
    """The marker at the point of the cut, matching the `## Comments` convention."""
    return f"…[{withheld} chars truncated here — use :full to fetch all]"


COMMENT_TAIL = 10
"""How many comments a capped render keeps — the most recent ones.

Which end gets kept is deliberate and is not a disclosure question: recency is
usually what a reviewer is after. #719 argued the opposite — that the oldest
comments carry the original objection — and that is a real argument, but it is a
*selection* argument and belongs in its own issue — #738, which also raises the
option of keeping both ends with the gap marked in the middle, the way `gl-mr`'s
byte-budgeted render already does. What #719 fixed is that the cut was invisible
whichever end it took.
"""


def comments_heading(shown: int, total: int) -> str:
    """The `## Comments (…)` line, stating the cut when there is one.

    An uncut list prints the bare count and nothing else, so the *absence* of a
    marker is itself the signal that the list is whole — the same contract
    `gl-issue` uses for its related-MR list (#635). A cut list names the exact
    number withheld and the flag that returns them.

    This exists because `gh-issue` had it and `gh-pr`, in the next file over,
    printed `## Comments (25)` above ten of them with nothing in between (#719).
    That is not a display preference: the header supplies a number the reader has
    every reason to trust, so a brief written from the render is confidently
    missing the fifteen comments that changed the deliverable. Two correct copies
    of a disclosure is how a third site forgets to have one, which is the whole
    argument this module exists to settle.
    """
    if shown >= total:
        return f"## Comments ({total})"
    withheld = total - shown
    return (
        f"## Comments ({shown} of {total} shown, {withheld} earlier "
        f"truncated — use :full to fetch all)"
    )


def comment_cut_notice(cap: int) -> str:
    """The marker for one comment body cut at the per-comment cap."""
    return f"…[truncated at {cap} chars — use :full]"
