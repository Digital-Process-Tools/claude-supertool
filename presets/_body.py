

































from __future__ import annotations


def cut(body: str, cap: int | None) -> tuple[str, int]:






    if cap is None or cap <= 0 or len(body) <= cap:
        return body, 0
    cut_at = body.rfind("\n", 0, cap)
    if cut_at <= 0:



        cut_at = cap
    shown = body[:cut_at]
    return shown, len(body) - len(shown)


def header_notice(shown: str, total: int, withheld: int) -> str:

    return (
        f"Body: TRUNCATED — {len(shown)} of {total} chars shown, "
        f"{withheld} withheld — use :full to fetch all"
    )


def cut_notice(withheld: int) -> str:

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






    if head < 0 or tail < 0 or len(comments) <= head + tail:
        return list(comments), 0
    kept_tail = comments[len(comments) - tail:] if tail else []
    return list(comments[:head]) + list(kept_tail), len(comments) - head - tail


def comments_gap_notice(hidden: int) -> str:







    word = "comment" if hidden == 1 else "comments"
    return f"…[{hidden} {word} hidden here — use :full to fetch all]"


def comments_heading(shown: int, total: int) -> str:















    if shown >= total:
        return f"## Comments ({total})"
    withheld = total - shown



    return (
        f"## Comments ({shown} of {total} shown, {withheld} hidden from the "
        f"middle — use :full to fetch all)"
    )


def comment_cut_notice(cap: int) -> str:

    return f"…[truncated at {cap} chars — use :full]"
