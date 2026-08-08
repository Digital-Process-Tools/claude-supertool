#!/usr/bin/env python3
"""One filter-keyed snapshot store, shared by every radar tier.

A tier's delta is only as honest as its key. Two populations sharing one
snapshot file report every member of the first as new and every member of the
second as gone, which is a delta column that lies — worse than no delta at all
(`gl_mrs.read_snapshot`, #486). That reasoning is not GitLab's; it is the
reasoning of anything that keeps a previous board, so it lives once here rather
than being retyped per tier. `gh_prs` re-deriving it would have been the second
copy, and a second copy is how a fixed defect comes back.

What is deliberately *not* here: what a member is, what counts as moved, and
what the key is made of. Those are the tier's semantics — a GitLab MR is keyed
by pipeline id and a GitHub PR by head SHA, and forcing one shape on both is the
bend this module exists to avoid.

What *is* here, besides the store, is `elided_note`: the disclosure a delta
board owes for the rows it did not print (#1022). Both tiers elide identically
and both used to elide silently, so the sentence lives once, next to the delta
whose consequence it is.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
import transport  # noqa: E402


def key(payload: Any) -> str:
    """Stable short hash of whatever identifies a population.

    Order-insensitive by construction: the caller normalises (sorted, deduped)
    and `sort_keys` does the rest, so `author=a,author=b` and the reverse are
    one population and one file — the same reason `gl_mrs.canonical_filter_string`
    exists (#476).
    """
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:12]


def path(prefix: str, digest: str) -> str:
    """Where the snapshot for one population lives. Read by `radar-state`."""
    return os.path.join(transport.STATE_DIR, f"{prefix}.{digest}.snapshot.json")


def read(prefix: str, digest: str, member: str) -> dict[str, Any] | None:
    """The previous board, or `None` on cold start.

    `None` and `{"<member>": {}}` are different answers and stay different: an
    absent file is "nobody has looked before", an empty one is "the population
    was empty last time". A tier that collapsed them would print a cold-start
    full board forever, or never.
    """
    try:
        with open(path(prefix, digest), encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict) or not isinstance(loaded.get(member), dict):
        return None
    return loaded


def elided_note(elided: list[str], total: int, noun: str, sigil: str,
                wider: str) -> list[str]:
    """Name the rows a delta board kept off itself. `[]` when it kept none.

    A delta board prints the rows that moved and the rows that are standing
    problems; everything else it drops. That is the point of a delta — but the
    footer counts the whole population, so the two disagree by construction and
    nothing used to say which to believe. Live (#1022): three rendered rows
    under `6 open | 2 failing | 4 running`, and the three that vanished were
    ordinary running PRs that simply had not changed since the previous tick.

    A partial board is strictly harder to notice than an empty one, because it
    looks like a working board — a maintainer reads three rows and merges as
    though three were all there was. So the count goes in the footer, where the
    arithmetic can be checked, and the identifiers go here, because a number a
    reader cannot resolve back to a row is not a disclosure.

    The empty return is load-bearing the same way `_unchecked_warning`'s is: on
    a board that printed everything, the *absence* of this line is the positive
    claim that nothing was held back. A line printed unconditionally is one the
    reader learns to skip.
    """
    if not elided:
        return []
    named = ", ".join(f"{sigil}{n}" for n in elided[:12])
    if len(elided) > 12:
        named += f", +{len(elided) - 12} more"
    return [f"radar: NOTE — {len(elided)} of {total} open {noun} are not on the "
            f"board: unchanged since the previous run and not a standing "
            f"problem ({named}). The footer counts all {total}, so rows plus "
            f"'unchanged not shown' is the whole population; `{wider}` prints "
            f"every row."]


def departed_note(departed: list[str], noun: str, sigil: str,
                  lookup: str) -> list[str]:
    """Name what left the board, and refuse to say why. `[]` when nothing did.

    The mirror of `elided_note`, and a harder case (#1024). A delta board knows
    exactly why it elided a row — it did the eliding. It does not know why a row
    left, because the snapshot records *membership in the filtered population*
    and nothing about how that membership ended. Merged, closed unmerged,
    reassigned to someone else, stripped of the label the filter selects on, or
    the filter itself changed between runs: all five arrive as the same absence.

    Both tiers used to render that absence as `N no longer open`, which is true
    for the first two and the opposite of the truth for the last three — the PR
    is open, still needs work, and the board just told the reader it landed.
    Distinguishing them costs a live lookup per departure that can itself fail,
    so it would need this three-state sentence anyway; the sentence alone costs
    no call and cannot be wrong.

    The identifiers are the actionable half: the reader who needs to know which
    of the five it was can run `lookup` on a named id, which they could not do
    against a bare count.
    """
    if not departed:
        return []
    # Sorted, because the cap makes *which* ids get named load-bearing and the
    # snapshot is written in the order the API returned its page. Unsorted, two
    # runs over the same departures can name different halves of them, and a
    # disclosure whose content depends on upstream ordering is one the reader
    # cannot check against anything.
    departed = sorted(departed,
                      key=lambda n: (0, int(n), "") if n.isdigit() else (1, 0, n))
    named = ", ".join(f"{sigil}{n}" for n in departed[:12])
    if len(departed) > 12:
        named += f", +{len(departed) - 12} more"
    plural = noun if len(departed) == 1 else f"{noun}s"
    return [f"radar: NOTE — {len(departed)} {plural} left this board since the "
            f"previous run ({named}): merged, closed, or still open and no "
            f"longer matching this board's filter. The snapshot records "
            f"membership, not how it ended, so this board does not guess — "
            f"`{lookup}` says which."]


def write(prefix: str, digest: str, entries: dict[str, Any], member: str) -> None:
    """Replace the snapshot atomically, or leave the old one in place.

    A half-written snapshot is a board that reports rows as changed which did
    not change, so the failure mode of an unwritable state dir is "no delta
    this run", never "a wrong delta next run".
    """
    target = path(prefix, digest)
    tmp = f"{target}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({member: entries}, f, indent=2)
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
