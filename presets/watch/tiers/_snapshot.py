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

import calendar
import hashlib
import json
import os
import sys
import time
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
                  lookup: str, capped: bool = False) -> list[str]:
    """Name what left the board, and refuse to say why. `[]` when nothing did.

    The mirror of `elided_note`, and a harder case (#1024). A delta board knows
    exactly why it elided a row — it did the eliding. It does not know why a row
    left, because the snapshot records *membership in the filtered population*
    and nothing about how that membership ended. Merged, closed unmerged,
    reassigned to someone else, stripped of the label the filter selects on, or
    — when the live fetch filled its page — never reached at all: they all
    arrive as the same absence.

    A changed filter is deliberately *not* on that list: the snapshot is keyed
    by filter, so widening one is a cold start with no previous entries to
    depart. Listing it would name a cause that cannot produce the observation.

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
    # `isdecimal`, not `isdigit`: the latter is True for superscripts and other
    # non-decimal digits, where `int()` raises. A real id never reaches that,
    # but a corrupted snapshot file does, and taking the whole board down over
    # a stray key is a worse failure than sorting it lexically.
    departed = sorted(departed,
                      key=lambda n: (0, int(n), "") if n.isdecimal() else (1, 0, n))
    named = ", ".join(f"{sigil}{n}" for n in departed[:12])
    if len(departed) > 12:
        named += f", +{len(departed) - 12} more"
    plural = noun if len(departed) == 1 else f"{noun}s"
    if capped:
        # The fourth history, and the one that breaks the claim entirely: the
        # live fetch is a single page, so an entry pushed off it by newer ones
        # is absent from `live` while being open and still matching. Calling
        # that a departure sends the reader to a lookup that shows it open, from
        # which the only available conclusion — "it stopped matching" — is also
        # wrong. So on a full page the set is not established and is not named
        # as one.
        return [f"radar: WARNING — {len(departed)} {plural} on the previous "
                f"snapshot are not on this one ({named}), and this board "
                f"cannot call that a departure: the live query returned a full "
                f"page, so an entry pushed past the page limit by newer ones "
                f"looks exactly like one that left. Merged, closed, no longer "
                f"matching this board's filter, or simply past the page limit "
                f"— `{lookup}` says which."]
    return [f"radar: NOTE — {len(departed)} {plural} left this board since the "
            f"previous run ({named}): merged, closed, or still open and no "
            f"longer matching this board's filter. The snapshot records "
            f"membership, not how it ended, so this board does not guess — "
            f"`{lookup}` says which."]


# ---------------------------------------------------------------------------
# how long has this entry held still? (#1025)
# ---------------------------------------------------------------------------

SINCE_KEY = "_since"

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def now_iso() -> str:
    """UTC, to the second, in the shape every other timestamp here wears."""
    return time.strftime(_TS_FMT, time.gmtime())


def facts(entry: Any) -> Any:
    """An entry without the bookkeeping the delta must not compare.

    Load-bearing, and the reason `SINCE_KEY` is a key on the entry rather than
    a second store: a timestamp inside the compared facts makes *every* row
    differ from its predecessor on *every* tick, which is not a staleness
    signal, it is the delta collapsing into a full board forever.
    """
    if not isinstance(entry, dict):
        return entry
    return {k: v for k, v in entry.items() if k != SINCE_KEY}


def stamp(entry: dict[str, Any], previous: Any, now: str | None = None
          ) -> dict[str, Any]:
    """`entry`, carrying when its facts were last observed to change.

    Carried forward while the facts hold still, re-stamped when any of them
    moves. So the age this yields is "unchanged for", measured from the last
    change this tier could see — not wall-clock since a run started, which is
    wrong for a matrix whose legs finish minutes apart, and not ticks, which
    would mean whatever the operator's radar cadence happens to be that day.

    What it is *not* is time since the last **leg** state change, which #1025
    argues is the truer signal. A tier stores a rollup word, not legs, so that
    fact is not available here; the difference matters when a matrix is
    genuinely progressing leg by leg under an unchanging `running` rollup, and
    such a board will read as older than it is. Said out loud rather than
    papered over — the threshold is the operator's to raise.
    """
    out = dict(entry)
    prior = previous.get(SINCE_KEY) if isinstance(previous, dict) else None
    held = bool(prior) and isinstance(prior, str) and facts(previous) == facts(entry)
    out[SINCE_KEY] = prior if held else (now or now_iso())
    return out


def unchanged_minutes(entry: Any, now: str | None = None) -> float | None:
    """Minutes this entry's facts have held still, or `None` when unknowable.

    `None` is a third state and stays one. An entry written before #1025
    landed carries no `SINCE_KEY`, and a corrupted one may carry something that
    is not a timestamp: both are "cannot tell", never zero. Reporting 0 there
    would be the house defect — an absence the tool produced rendered as an
    absence in the world, on the one board that exists to catch a PR nothing
    else is reporting.

    Self-heals in one write: the next `stamp` puts a real timestamp on it. So
    the cost of the unknown state is that a run wedged before the upgrade is
    first named one threshold after it, once, rather than never.
    """
    if not isinstance(entry, dict):
        return None
    raw = entry.get(SINCE_KEY)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        then = calendar.timegm(time.strptime(raw, _TS_FMT))
        current = calendar.timegm(time.strptime(now or now_iso(), _TS_FMT))
    except ValueError:
        return None
    return max(0.0, (current - then) / 60.0)


def unchanged_label(minutes: float, state: str) -> str:
    """`pending 5h unchanged` — the state observed, and how long it held.

    The state word is the one the forge reported, never a fixed literal. A
    GitLab pipeline stuck at `pending` never started; printing it as `running`
    would have the board tell a maintainer something other than what it
    observed, on the one row that exists precisely because nothing else was
    going to mention it. That is the defect this file's whole vocabulary is
    against, arriving inside the fix for it.

    Lives here rather than in each tier for the reason the module docstring
    gives: both tiers render this identically, and a second copy is how a fixed
    defect comes back — `elided_note` and `departed_note` are the precedent.
    """
    unit = f"{int(minutes // 60)}h" if minutes >= 120 else f"{int(minutes)}m"
    return f"{state or 'in progress'} {unit} unchanged"


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
