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


COMMENT_HEAD = 3
COMMENT_TAIL = 7
"""How many comments a capped render keeps, and from which ends.

Ten, as before #738 — the budget is unchanged and no caller pays more context
for this. What changed is that they are no longer all taken from one end.

#719 kept the ten most recent and #738 asked whether the ten oldest were the
better choice, since the opening comments carry the original objection, the
design decision and the "do not merge until X". Both sides are right about
*different comments*: the head carries the objection that opened the thread and
the tail carries the resolution that closed it. So a cap that takes one end
guarantees that on every long thread one of the two load-bearing regions is
gone — and, the part that makes this a defect rather than a preference, the
reader cannot tell which, because a thread whose opening never mattered renders
identically to one whose opening was the whole point. That is this repo's
standing shape: an absence produced by the tool, read as an absence in the
world.

#738 asked for a measurement to settle it and the measurement is not available.
Over the whole tracker on 2026-08-11 the busiest thread in this repository had
six comments and the cap had never once fired, so there is no local corpus in
which to count where the load-bearing comment sits; these ops read other repos
through `repo:OWNER/NAME`, where 25-comment threads are ordinary. With no
evidence for either end, the design that does not require choosing one is the
answer.

It is not a new convention either. `gl-mr`'s `_budgeted_comments` has kept a
head and a recency tail with an inline gap marker since it was written, so the
two GitHub ops converge on a shipped shape instead of drifting into a third.
The split is tail-weighted because "where does this stand" stays the commoner
question, and three is enough to carry a thread that opens with an objection
and a reply to it.
"""


def comment_window(comments: list, head: int = COMMENT_HEAD,
                   tail: int = COMMENT_TAIL) -> tuple:
    """Return ``(shown, hidden)`` — the head and tail kept, and what fell out.

    ``hidden`` is a count, not a slice: no caller needs the dropped comments,
    every caller needs to be able to say how many there were. ``hidden == 0``
    means the list is whole and no disclosure is owed, matching `cut()`.
    """
    if head < 0 or tail < 0 or len(comments) <= head + tail:
        return list(comments), 0
    kept_tail = comments[len(comments) - tail:] if tail else []
    return list(comments[:head]) + list(kept_tail), len(comments) - head - tail


def comments_gap_notice(hidden: int) -> str:
    """The marker printed between the head and the tail.

    The header states the count, but a header alone leaves the reader unable to
    see that two adjacent comments are not consecutive — which is the re-read
    the header was added to prevent. Wording follows `cut_notice`, the
    convention already used at the point of a body cut.
    """
    word = "comment" if hidden == 1 else "comments"
    return f"…[{hidden} {word} hidden here — use :full to fetch all]"


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
    # "earlier truncated" was true while the cut took the head off. Since #738
    # it takes the middle, and a disclosure that names the wrong end is the
    # defect #719 fixed wearing the fix's own clothes.
    return (
        f"## Comments ({shown} of {total} shown, {withheld} hidden from the "
        f"middle — use :full to fetch all)"
    )


def comment_cut_notice(cap: int) -> str:
    """The marker for one comment body cut at the per-comment cap."""
    return f"…[truncated at {cap} chars — use :full]"
