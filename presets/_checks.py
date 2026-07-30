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

import re
from collections import Counter
from typing import Iterable, List, Sequence

# Marker appended whenever the checks are not unanimously successful. Its job
# is to make "not green" unmissable at a glance, so the rule stays blunt:
# anything other than every-single-check-passed earns it.
NOT_GREEN = "⚠ NOT ALL GREEN"

# Printed instead of a zeroed tally when there is nothing to count *and*
# nothing has been established about why. It must not be confusable with
# "checks exist but I could not classify them", so it says what is absent
# rather than counting to zero. A caller that can reach evidence about the
# absence renders `absence()` instead — see its docstring for why this line on
# its own is not an answer.
NO_CHECKS = "none reported — no check runs on this commit"

# How long a first check run may take to appear before its absence stops being
# explained by GitHub's creation latency. Measured on this repo in #585: 99s,
# 165s, ~2min and 4.5min from PR-open to first run created. 15min is ~3x the
# worst observed, deliberately generous — the window's job is to make "not yet"
# the only reading while a run could still plausibly be on its way, so erring
# long costs a waiting reader nothing, while erring short would put the word
# UNKNOWN on a perfectly healthy PR.
CHECK_CREATION_GRACE_SECS = 900

# Printed instead of a bare `none` when a GitLab MR carries no pipeline (#587).
# `Pipeline: none` is the GitLab spelling of the sentence #585 removed: it reads
# as "there is no CI on this ref" — the *never* leg — when it is equally the
# just-pushed leg. There is deliberately no grace window here: the ~15min above
# is measured GitHub creation latency, and inventing a GitLab equivalent with no
# measurement behind it would be guessing in the shape of evidence. So this leg
# only ever declines, and a measured window can be added later.
NO_PIPELINE = (
    "none reported — whether one is still coming is UNKNOWN. GitLab makes a "
    "pipeline at push time, so a missing one can mean no job matched this ref, "
    "and it can equally mean the head was just pushed or the MR's pipeline is "
    "not attached to this payload. Check the MR's Pipelines tab."
)

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


def _duration(secs: int) -> str:
    """A bare age — '45s', '2m', '2h', '3d'. No 'ago': the caller supplies it."""
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


# PR states in which no `pull_request` event can fire for the head ref any
# more, mapped to what would have to change for one to. A run genuinely still
# in flight is excluded by the grace window before this map is consulted.
_TERMINAL_PR_STATES = {
    "MERGED": "again",
    "CLOSED": "unless it is reopened",
}


def absence(pr_state: object, age_secs: int | None,
            grace_secs: int = CHECK_CREATION_GRACE_SECS) -> tuple[str, str]:
    """Render zero check runs as one of three states (#585).

    Returns `(checks_text, mergeable_note)`. `NO_CHECKS` on its own answered a
    merge question with one sentence covering two opposite situations — the run
    has not been created yet (waiting is correct) and the run is never coming
    (waiting is a deadlock). Both read as "not yet" to somebody waiting to
    merge, and both were read that way, by two readers ten minutes apart.

    The evidence is timestamps and PR state, not workflow configuration:

    * **not yet** — the head commit is younger than `grace_secs`, so GitHub's
      own creation latency explains the absence. Nothing else is claimed.
    * **none will be created** — the head commit is older than the window
      *and* the PR is merged or closed. This is the empirical leg, and it is
      why no `on:` block is parsed here: a push event for this ref has already
      fired and produced no run, and no `pull_request` event can fire again.
      Inferring the same thing from `.github/workflows/*` would be inferring
      it from files that need not be the ones on the PR's head ref.
    * **UNKNOWN** — anything else. An *open* PR sitting well past the window
      with zero runs is overdue, not decided: an event can still fire for it,
      so the age is printed and the conclusion is declined. `docs/validators.md`
      ("Declining instead of guessing"): a checker that cannot answer says so.
      A failed age lookup lands here too, never in the leg above.
    """
    state = normalize(pr_state)
    window = f"~{max(1, grace_secs // 60)}min"

    if age_secs is None:
        return (
            "none reported — no check runs on this commit, and whether one is "
            "still coming is UNKNOWN: could not establish when the head commit "
            "landed. Check the PR's Checks tab.",
            " — no checks reported, and whether any are coming is UNKNOWN",
        )

    age = _duration(age_secs)

    if age_secs <= grace_secs:
        return (
            f"none yet — head commit {age} old, inside the {window} window in "
            "which a first run has always appeared; a run is still expected",
            " — no checks yet, a run is still expected",
        )

    if state in _TERMINAL_PR_STATES:
        tail = _TERMINAL_PR_STATES[state]
        return (
            f"none, and none will be created — head commit {age} old and still "
            f"zero runs, and the PR is {state}, so no pull_request event will "
            f"fire for this ref {tail}. Waiting will not change this.",
            f" — no checks, and none will be created (PR is {state})",
        )

    return (
        f"none reported — head commit {age} old and still zero runs, past the "
        f"{window} window in which a first run normally appears; the PR is "
        f"{state}, so an event could still fire and whether any workflow covers "
        "this ref is UNKNOWN. Check the PR's Checks tab.",
        " — no checks reported, and whether any are coming is UNKNOWN",
    )


_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def is_full_sha(value: object) -> bool:
    """True only for a full 40-hex object name.

    Guards every use of a platform-supplied head SHA as a *local* revision
    argument. `git log -1 HEAD` and `git log -1 master` both succeed and both
    date a commit that is not the PR's head, so a `headRefOid` carrying a
    revision expression has to be refused rather than resolved. Dating the
    wrong commit and captioning it as the PR head's age is #585's defect one
    layer along, with more confidence attached.
    """
    return bool(_FULL_SHA.match(str(value or "").strip().lower()))


def head_relation(local_sha: object, pr_head_sha: object,
                  number: object = None) -> str:
    """Which commit the `Checks:` line describes. `''` when it is your HEAD.

    The state `gh-pr` does not have (#587). `gh-pr` is handed a PR number and
    prints checks for that PR. `git-status` resolves a PR *by branch* while
    standing in a working tree whose `HEAD` may be ahead of, behind, or
    unrelated to the PR's head SHA — so its check summary can be a true
    statement about a commit the reader has already moved past. `Checks: 12
    total: 12 passed` then reads as "your work is green", which is the opposite
    of the truth when the two unpushed commits under your cursor are what you
    were asking about.

    Silence is reserved for the two SHAs being *established equal*. An
    unestablished relation states UNKNOWN rather than printing nothing, because
    nothing is read as "same commit" — the absence of a check rendering as a
    passed one, which is this repository's house defect.
    """
    local = str(local_sha or "").strip().lower()
    remote = str(pr_head_sha or "").strip().lower()

    if is_full_sha(local) and is_full_sha(remote):
        if local == remote:
            return ""
        pointer = f"gh-pr:{number}" if str(number or "").strip() not in ("", "?") else "gh-pr"
        return (
            f"Checks commit: PR head {remote[:7]} — NOT your local HEAD "
            f"{local[:7]}. The Checks line above is about the PR's head commit, "
            f"not the commit you are standing on. `{pointer}` for that commit."
        )

    l_disp = local[:7] if is_full_sha(local) else "unestablished"
    r_disp = remote[:7] if is_full_sha(remote) else "unestablished"
    return (
        f"Checks commit: PR head {r_disp}, local HEAD {l_disp} — whether the "
        "Checks line above is about the commit you are standing on is UNKNOWN."
    )
