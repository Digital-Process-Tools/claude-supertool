#!/usr/bin/env python3
"""One tally for every CI check summary supertool prints.

The tally this replaces knew three buckets — passed, failed, pending — and
silently discarded every check whose state fell outside them. On PR #452 that
turned a run which had concluded `failure` into:

    Checks: 10 passed, 0 failed, 0 pending

Twelve legs existed; two were CANCELLED. `0 failed, 0 pending` reads as
"everything is accounted for and nothing is outstanding", which is the one
reading that was definitely wrong — and it was printed next to `Mergeable:
yes`, in the merge-decision path.

The fix is arithmetic, not enumeration. `summarize()` opens with the number of
checks it was handed and every term after it sums back to that number, so a
state nobody has taught this module about surfaces as its own named term
instead of evaporating. Enumerating states one at a time would always trail
whatever GitHub or GitLab adds next; the sum cannot.
"""
from __future__ import annotations

from collections import Counter
from typing import Iterable, List, Sequence

# Marker appended whenever the checks are not unanimously successful. Its job
# is to make "not green" unmissable at a glance, so the rule stays blunt:
# anything other than every-single-check-passed earns it.
NOT_GREEN = "⚠ NOT ALL GREEN"

# Printed instead of a zeroed tally when there is nothing to count. It must not
# be confusable with "checks exist but I could not classify them", so it says
# what is absent rather than counting to zero.
NO_CHECKS = "none reported — no check runs on this commit"

PASSED_STATES = frozenset({"SUCCESS"})

# A check in any of these is a red check. TIMED_OUT and ACTION_REQUIRED belong
# here and nowhere near SKIPPED: a job that ran out of wall clock produced no
# verdict, and one waiting on a human approval is blocking.
FAILED_STATES = frozenset({
    "FAILURE", "FAILED", "ERROR", "STARTUP_FAILURE", "TIMED_OUT",
    "ACTION_REQUIRED",
})

# Genuinely still moving, or not yet started.
PENDING_STATES = frozenset({
    "IN_PROGRESS", "RUNNING", "QUEUED", "PENDING", "WAITING", "REQUESTED",
    "EXPECTED", "CREATED", "SCHEDULED", "PREPARING",
})

# Leftover states a reviewer can reasonably shrug at. Everything else that
# falls through the three buckets — CANCELLED/canceled, STALE, and any state
# added after this file was written — counts as red on the triage boards,
# because "I do not recognise this" must never default to "fine".
BENIGN_STATES = frozenset({"SKIPPED", "NEUTRAL", "MANUAL"})

_UNKNOWN = "UNKNOWN"


def normalize(state: object) -> str:
    """Uppercase a raw platform state. Empty/None becomes UNKNOWN, not ''."""
    s = str(state or "").strip().upper()
    return s or _UNKNOWN


def bucket(state: object) -> str:
    """Classify one state as 'passed' | 'failed' | 'pending' | 'other'."""
    s = normalize(state)
    if s in PASSED_STATES:
        return "passed"
    if s in FAILED_STATES:
        return "failed"
    if s in PENDING_STATES:
        return "pending"
    return "other"


def is_red(state: object) -> bool:
    """True when a triage board should sort/filter this as failing.

    Unrecognised states are red on purpose: a board that sorts failing-first
    cannot surface what its classifier decided to call harmless.
    """
    b = bucket(state)
    if b == "failed":
        return True
    return b == "other" and normalize(state) not in BENIGN_STATES


def github_state(check: dict) -> str:
    """The one state token for a `statusCheckRollup` entry.

    Check runs carry `conclusion` once finished and `status` while moving;
    legacy commit statuses carry `state` instead. An entry with none of them
    is still a check, so it resolves to UNKNOWN rather than disappearing.
    """
    if not isinstance(check, dict):
        return _UNKNOWN
    for key in ("conclusion", "status", "state"):
        val = check.get(key)
        if val:
            return normalize(val)
    return _UNKNOWN


def github_states(checks: object) -> List[str]:
    """Every rollup entry as a state token — length always == len(checks)."""
    if not isinstance(checks, list):
        return []
    return [github_state(c) for c in checks]


def _label(state: str) -> str:
    """Term label for a leftover state. Lowercase, underscores kept."""
    return normalize(state).lower()


def summarize(states: Sequence[str] | Iterable[str]) -> str:
    """Render the summary that follows `Checks: `.

    Always opens with `N total` and every count after it sums to N, so the
    line can be audited by arithmetic instead of by trusting the labels::

        12 total: 10 passed, 0 failed, 0 pending, 2 cancelled ⚠ NOT ALL GREEN
        12 total: 0 passed, 0 failed, 12 pending ⚠ NOT ALL GREEN
        3 total: 3 passed, 0 failed, 0 pending
        none reported — no check runs on this commit
    """
    tokens = [normalize(s) for s in states]
    total = len(tokens)
    if total == 0:
        return NO_CHECKS

    buckets = Counter(bucket(t) for t in tokens)
    parts = [
        f"{buckets.get('passed', 0)} passed",
        f"{buckets.get('failed', 0)} failed",
        f"{buckets.get('pending', 0)} pending",
    ]
    leftovers = Counter(_label(t) for t in tokens if bucket(t) == "other")
    for label, count in sorted(leftovers.items(), key=lambda kv: (-kv[1], kv[0])):
        parts.append(f"{count} {label}")

    line = f"{total} total: " + ", ".join(parts)
    if buckets.get("passed", 0) != total:
        line += f" {NOT_GREEN}"
    return line


def summarize_github(checks: object) -> str:
    """`summarize()` over a raw `statusCheckRollup` list."""
    return summarize(github_states(checks))


def all_green(states: Sequence[str] | Iterable[str]) -> bool:
    """True only when at least one check exists and every one of them passed."""
    tokens = [normalize(s) for s in states]
    return bool(tokens) and all(bucket(t) == "passed" for t in tokens)
