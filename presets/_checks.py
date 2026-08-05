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

# Public spelling of the state above. A caller that must tell *unreadable*
# from *resolved* — `gh-run`'s step counter, #803 — has to name this state,
# and re-deriving it as a literal is the bug that op was fixing.
UNKNOWN = _UNKNOWN


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


_JOB_ID_IN_URL = re.compile(r"/job/([0-9]+)(?:[/?#]|$)")

# A check run's own page: `https://<host>/<owner>/<repo>/runs/<check-run-id>`.
# Exactly two path segments before `/runs/` — which is what keeps an Actions
# leg's `.../<o>/<r>/actions/runs/<run>/job/<job>` out of it. The integer after
# `/runs/` there is the *run* id, and printing it under a check run's label
# would be a wrong id wearing a confident header, which is the defect class
# #827 is about, manufactured fresh (#827).
#
# The shape is host-agnostic on purpose so GitHub Enterprise matches. The
# residual exposure — a foreign CI whose URLs happen to be shaped
# `host/a/b/runs/N` — is the same one `_JOB_ID_IN_URL` has always carried, and
# it is bounded the same way: the id is only ever *offered* to the reader as
# the next op to run, and that op 404s with a stated cause rather than
# answering about the wrong thing.
_CHECK_ID_IN_URL = re.compile(
    r"^https?://[^/]+/[^/]+/[^/]+/runs/([0-9]+)(?:[/?#]|$)")


def github_job_id(check: dict) -> str:
    """The Actions job id parsed from a CheckRun's `detailsUrl`, '' if absent.

    Rides along for free — no extra GraphQL/API call — because the id was
    already sitting in a field `gh-pr` fetches on every call (#619). It is
    what makes the leg named by `named_disclosure()` reachable with
    `gh-job:<id>:fail` instead of a `gh api .../actions/jobs` detour: the
    step after "which leg" is always "what did it say".

    Only `CheckRun` entries carry an Actions job id in their `detailsUrl`
    (`.../actions/runs/<run>/job/<job>`); legacy commit statuses point at
    whatever external system set them and never match, so they return ''
    rather than a wrong id.
    """
    if not isinstance(check, dict):
        return ""
    m = _JOB_ID_IN_URL.search(str(check.get("detailsUrl") or ""))
    return m.group(1) if m else ""


def github_check_ref(check: dict) -> tuple[str, str]:
    """`(kind, id)` for a rollup leg — `("job"|"check", id)`, or `("", "")`.

    GitHub reports CI through two id namespaces and a rollup mixes them, so
    "which id is this" is not answerable without also saying which namespace
    it belongs to. Both answers ride on `detailsUrl`, a field `gh-pr` already
    fetches, so this costs no request (#619's trade, one URL shape wider).

    **The second shape was there all along and nothing read it.** #821's docs
    state that "the check-run id rides on `detailsUrl` only for Actions legs".
    Read against the real API on 2026-08-05, the `github-advanced-security`
    leg of PR 821 carries
    `detailsUrl: https://github.com/Digital-Process-Tools/claude-supertool/runs/92264897684`
    — and `check-runs/92264897684` resolves to that same CodeQL leg. The id
    was in the payload; the parser only knew `/job/<id>`.

    An Actions URL contains `/runs/<run-id>` too, and that integer names a run
    rather than a check run. What keeps it out is the **anchor** in
    `_CHECK_ID_IN_URL` — exactly two path segments before `/runs/` — not the
    order of the two branches below; a mutation run that swapped them left the
    suite green, which is the evidence that the anchor is carrying it. The
    order stays as defence in depth and costs nothing. A leg matching neither
    shape — a legacy commit status pointing at someone else's server — returns
    `("", "")` rather than a wrong id.
    """
    if not isinstance(check, dict):
        return ("", "")
    url = str(check.get("detailsUrl") or "")
    job = _JOB_ID_IN_URL.search(url)
    if job:
        return ("job", job.group(1))
    run = _CHECK_ID_IN_URL.search(url)
    if run:
        return ("check", run.group(1))
    return ("", "")


def github_named_states(checks: object) -> List[tuple[str, str, str, str]]:
    """`(name, state, kind, id)` per leg — length == len(checks).

    The input `named_disclosure()` is built for: everything `summarize()`
    already counts, plus the name and the namespaced id it throws away.

    `kind` is carried rather than inferred downstream because the two
    namespaces mint from one integer sequence — an id alone cannot say which
    op reads it, and a default would guess.
    """
    if not isinstance(checks, list):
        return []
    out: List[tuple[str, str, str, str]] = []
    for c in checks:
        if isinstance(c, dict):
            name = str(c.get("name") or c.get("context") or "?")
            kind, ident = github_check_ref(c)
            out.append((name, github_state(c), kind, ident))
        else:
            out.append(("?", _UNKNOWN, "", ""))
    return out


# Legs listed per non-passing group before the line switches to `+N more`.
# `summarize()`'s own arithmetic is unbounded — it has to be, the count has
# to sum to the total — but *naming* fourteen legs one poll after another is
# the failure this repo already has a word for (#605's disclosure cap): the
# thing meant to answer "which one" becomes the thing that blows the budget.
# 5 is not measured, it is chosen to match the issue's own worked example
# (5 failing legs printed with no elision) — small enough that the terse
# `:status` form stays terse in the common case, generous enough that a
# typical single-leg or single-platform failure is never truncated.
NAMED_CAP = 5


def named_disclosure(
    entries: Sequence[tuple[str, str, str, str]], cap: int = NAMED_CAP
) -> List[str]:
    """One line per non-pass/non-pending state, naming every leg (#619).

    `summarize()` answers "how many"; this answers "which". Two buckets are
    deliberately excluded, for opposite reasons:

    * **passed** — needs no naming, it is not the reader's problem.
    * **pending** — resolves itself. Naming eight still-queued legs on every
      poll is the noise the terse form exists to avoid, and nothing about a
      pending leg tells the reader what to do next.

    Everything else — `failed`, and every `other`-bucket state
    (`CANCELLED`, `SKIPPED`, `NEUTRAL`, `TIMED_OUT`, `ACTION_REQUIRED`, and
    anything this module has not been taught about yet) — is named, grouped
    under its own label, never merged into a bare count. That fold is
    `summarize()`'s own docstring, one layer up: `0 failed, 0 pending` read
    as "nothing outstanding" when two legs were silently CANCELLED. Naming
    them here is the same arithmetic promise applied to the "which" question
    instead of the "how many" one.

    Grouping matches `summarize()`'s buckets exactly — every `FAILED_STATES`
    member (including `TIMED_OUT`/`ACTION_REQUIRED`) lands under one
    `failed:` line, same as the tally's `N failed` count — so the two lines
    can be read side by side without arithmetic drift between them.

    Each group is capped at `cap` legs, oldest-first as handed in, with a
    trailing `+N more` — this repo's established disclosure vocabulary
    (#605) — when the group is larger.

    A leg carrying an id prints `name (job #id)` or `name (check #id)`; one
    without prints its bare name. **The word is part of the answer, not
    decoration** (#827): the reader's next move is one op on that integer, and
    the two namespaces overlap in one direction, so a `check #` labelled `job #`
    sends them to a 404 and reads as an absence.
    """
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for name, state, kind, ident in entries:
        b = bucket(state)
        if b in ("passed", "pending"):
            continue
        label = "failed" if b == "failed" else _label(state)
        groups.setdefault(label, []).append((name, kind, ident))

    lines: List[str] = []
    for label in sorted(groups):
        items = groups[label]
        shown = items[:cap]
        parts = [f"{n} ({k} #{i})" if i and k else n for n, k, i in shown]
        text = ", ".join(parts)
        if len(items) > cap:
            text += f", +{len(items) - cap} more"
        lines.append(f"  {label}: {text}")
    return lines


def label(state: str) -> str:
    """Term label for a leftover state. Lowercase, underscores kept.

    Public because a caller rendering a *second* tally next to `summarize()`'s
    must spell its terms identically or the two drift on screen (#803):
    `gh-run` prints `## Failed jobs (6) — 3 failed, 2 cancelled, 1 unknown`
    under a header saying `11 total: … 3 failed … 2 cancelled …`, and the only
    thing making those the same numbers is that both come from here.
    """
    return normalize(state).lower()


# The spelling used inside this module since #445.
_label = label


# Appended to the `Checks:` line when the rollup carried fewer legs than the
# run declares. Deliberately a second marker rather than a reworded
# `NOT_GREEN`: "not all green" is a claim about the legs that were read, and
# this is a claim about the ones that were not. Both can be true at once and
# a reader deciding a merge needs both.
INCOMPLETE_MARK = "⚠ INCOMPLETE"

# Appended when the declared count could not be established at all. Not
# NOT_GREEN — that asserts red, and nothing here establishes red. It asserts
# only that the completeness of the tally is UNKNOWN, which is what
# `docs/validators.md` §"Declining instead of guessing" requires of a check
# that cannot answer.
UNVERIFIED_MARK = "⚠ TALLY UNVERIFIED"


def _legs(n: int) -> str:
    """`leg` / `legs`. A tally that says "1 legs" reads as machine output, and
    machine output is what a reader skims past."""
    return "leg" if n == 1 else "legs"


def shortfall(found: int, declared: int | None,
              missing: Sequence[str] = (),
              cap: int = NAMED_CAP) -> tuple[str, List[str]]:
    """Reconcile legs *read* against legs the run *declares* (#724).

    Returns `(marker, lines)` — `("", [])` when the two agree, so the common
    path costs nothing and the disclosure keeps its signal.

    `summarize()` promises that its terms sum to the number of checks it was
    handed. That promise held on PR #715 while the answer was wrong: nine
    entries arrived, `8 + 0 + 1 = 9` summed, the header said `9 total`, and a
    fourteen-leg matrix went unremarked. Internal consistency cannot detect a
    missing *input* — only a second, independent count of what should have
    arrived can, which is what `declared` is.

    Three outcomes, and the two unhappy ones are deliberately different:

    * `declared is None` — the count could not be established. Declined, not
      guessed. Assuming `declared == found` restores exactly the silence this
      exists to break; assuming the larger number invents legs and trades a
      loud failure for a quiet one.
    * `declared > found` — a proven shortfall. Both numbers are stated, in
      that order, and the legs that never arrived are named when known.
    * `declared <= found` — reconciled. `<` and not just `==` because a rollup
      legitimately carries checks belonging to no Actions run at all (external
      CI, legacy commit statuses); those are extra, never missing.
    """
    if declared is None:
        return (UNVERIFIED_MARK, [
            f"  unverified: {found} {_legs(found)} read, but how many the run declares "
            "could not be established, so whether these are all of them is "
            "UNKNOWN. Count by hand with `gh run view <run-id> --json jobs` "
            "before treating this as a merge signal."
        ])

    if declared <= found:
        return ("", [])

    gap = declared - found
    marker = f"{INCOMPLETE_MARK} — {found} of {declared} legs read"
    named = [n for n in missing if n]
    if named:
        shown = ", ".join(named[:cap])
        if len(named) > cap:
            shown += f", +{len(named) - cap} more"
        detail = f"not read: {shown}"
    else:
        detail = (f"not read: {gap} {_legs(gap)} the run declares "
                  "are absent, names UNKNOWN")
    return (marker, [
        f"  {detail} — this tally describes {found} of {declared} legs and is "
        "not a merge signal. GitHub re-creates check runs during a partial "
        "re-run, so re-running the op usually settles it."
    ])


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
            grace_secs: int = CHECK_CREATION_GRACE_SECS,
            mergeable: object = None) -> tuple[str, str]:
    """Render zero check runs as one of four states (#585, #594).

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
    * **conflicting** — `mergeable` is exactly `CONFLICTING` (#594). A
      `pull_request` workflow runs against `refs/pull/N/merge`, which GitHub
      builds by merging head into base; it cannot be built while the merge
      conflicts, so this PR has zero runs *permanently* and a rebase is the only
      thing that changes it. Rendered as the UNKNOWN leg it told the reader to
      wait or go look at a tab that will stay empty.

      Only an exact match claims this, and nothing else in this function claims
      the opposite. GitHub returns `UNKNOWN` while it recomputes mergeability,
      so an unresolved state falls through to the three legs above — all of
      which are silent about conflicts — rather than reading as "not
      conflicted". Neither confident claim is reachable from a state GitHub has
      not settled.
    """
    if str(mergeable or "").strip().upper() == "CONFLICTING":
        return (
            "none, and none will be created until the conflict is resolved — "
            "mergeable state is CONFLICTING, so GitHub cannot build "
            "refs/pull/N/merge for a pull_request run to execute against. "
            "Rebase — waiting will not change this.",
            " — no checks, and none will be created (mergeable is CONFLICTING)"
            " — rebase",
        )

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


# Printed instead of an issue number when the body declares no closing
# reference (#591). The line it replaces made the keyword optional, so the
# pattern reduced to "the first `#N` anywhere in the body" and `Issue: #263`
# was printed for a PR that closed #591 — the body had cited #263 as a
# precedent. A stated wrong number gets acted on; a missing one sends the
# reader to the body, so the old shape failed in the worse direction. This is
# the third state in `git-status`' terms, the same discrimination #587 made one
# line above: an answer, a finding, and *nothing declared* are three things.
#
# The claim is scoped to the body, which is all this line reads. "What merging
# closes is UNKNOWN" was the first wording and it overstates twice: a PR can
# also be linked through GitHub's Development panel, which closes an issue on
# merge and appears nowhere in the body, and `UNKNOWN` is the word the check
# tally reserves for a state it declined to conclude — reusing it here put a
# second, unrelated UNKNOWN into `git-status` output that reads as a check
# verdict.
NO_CLOSING_REF = (
    "none declared in the body — no closing keyword (Closes/Fixes/Resolves "
    "#N) bound to an issue number. A bare #N mention is not a closing "
    "reference to GitHub and is not reported as one here; a link made through "
    "the PR's Development panel is not in the body and is invisible to this "
    "line."
)

# GitHub's own set, verbatim: close/closes/closed, fix/fixes/fixed,
# resolve/resolves/resolved. Inventing a narrower list is not the safe move it
# looks like — GitHub's set is what decides whether merging the PR closes the
# issue, so a shorter one here silently drops issues that really do get closed,
# and a divergence between what we print and what the merge does is its own
# trap.
_CLOSING_KEYWORD = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"

# The keyword must be bound to *its own* number. The separator is horizontal
# whitespace and an optional colon — `Closes: #N`, `closes  #N` and `Closes#N`
# all close the issue on GitHub — and deliberately not `\s*`, which spans
# newlines: "This fixes" / blank line / "#263 is a precedent" would then
# extract #263 from a sentence whose keyword never names a number, which is the
# optional-keyword bug in a thinner disguise.
#
# Three reference shapes, because GitHub honours three: `#N`, `GH-N` and
# `owner/repo#N` — plus the full issue URL, which is `owner/repo#N` spelled
# long. A cross-repo reference keeps the repo it names: `#5` and `octo/other#5`
# are different issues, and flattening one into the other would hand a caller a
# number to resolve against the wrong repository.
_CLOSING_REF = re.compile(
    r"\b" + _CLOSING_KEYWORD + r"\b[ \t]*:?[ \t]*(?:"
    r"https?://github\.com/(?P<u_owner>[\w.\-]+)/(?P<u_repo>[\w.\-]+)/issues/(?P<u_num>\d+)"
    r"|(?P<x_owner>[\w.\-]+)/(?P<x_repo>[\w.\-]+)#(?P<x_num>\d+)"
    r"|(?:GH-|#)(?P<num>\d+)"
    r")",
    re.IGNORECASE,
)


# GitHub does not honour a closing reference inside a code span, a fenced block
# or an HTML comment — established by dogfooding rather than reasoned. PR #600's
# body cites `Closes #571 and closes #572` inside a code span as an *example* of
# the rendering, and GitHub's own `closingIssuesReferences` for that PR returned
# {571, 591}: 571 from a prose sentence elsewhere in the body, 572 from nowhere.
# One variable, one observation — the span was skipped. Matching it here would
# claim an issue the merge will not close, which is this whole change's defect
# pointed at a different input.
#
# The regions are removed and replaced by a newline, not skipped over, so text on
# either side of a removal cannot fuse into a match that neither half contained.
# An unterminated fence runs to end-of-body, which is what a markdown renderer
# does with one too.
#
# Four-space indented code blocks are deliberately NOT handled: telling one from
# a nested list continuation needs a real block parser, and guessing wrong would
# delete body prose and drop a genuine closing reference — the failure direction
# this change exists to remove. Erring toward matching is the safe side here.
_HTML_COMMENT = re.compile(r"<!--[\s\S]*?-->")
_FENCED = re.compile(r"^[ \t]*(```+|~~~+)[\s\S]*?(?:^[ \t]*\1[ \t]*$|\Z)",
                     re.MULTILINE)
_CODE_SPAN = re.compile(r"(`+)[\s\S]*?\1")


def _strip_unhonoured(text: str) -> str:
    """Drop the regions GitHub's own reference parser does not read."""
    for pattern in (_HTML_COMMENT, _FENCED, _CODE_SPAN):
        text = pattern.sub("\n", text)
    return text


def closing_issue_refs(body: object) -> List[str]:
    """Every issue this body declares it closes, in order, deduped.

    `[]` means the body declares none — render `NO_CLOSING_REF`, never the
    first number that can be found. That fallback is the defect (#591): a PR
    body routinely cites issues it does *not* close (a precedent, a sibling
    filed separately, a related discussion), and the well-written body is the
    one most likely to cite context before naming its own subject.

    Returns display forms, not bare integers: `"#591"` for this repository and
    `"owner/repo#5"` for another one. The distinction is load-bearing for any
    caller that goes on to *fetch* the issue — `gh issue view 5` resolves 5
    against the current repo, so a cross-repo number resolved here would print
    a different issue's title under this PR's closing reference.

    Every reference is returned, not the first. A PR closing two issues is
    normal (#584 closed #571 and #572) and picking one is the same defect with
    a smaller blast radius.

    Code spans, fenced blocks and HTML comments are removed before matching,
    because GitHub does not read them either — see `_strip_unhonoured`.
    """
    text = _strip_unhonoured(str(body or ""))
    if not text:
        return []

    refs: List[str] = []
    for m in _CLOSING_REF.finditer(text):
        if m.group("num"):
            ref = f"#{m.group('num')}"
        elif m.group("x_num"):
            ref = f"{m.group('x_owner')}/{m.group('x_repo')}#{m.group('x_num')}"
        else:
            ref = f"{m.group('u_owner')}/{m.group('u_repo')}#{m.group('u_num')}"
        if ref not in refs:
            refs.append(ref)
    return refs


def linked_issue_line(refs: Sequence[str]) -> str:
    """The one `Issue:`/`Issues:` line, so the two renderers cannot drift.

    Plural when there is more than one: `Issue: #571, #572` reads as one issue
    with a stray number attached. The absence leg is a printed sentence rather
    than a skipped line, because a missing `Issue:` line is indistinguishable
    from a renderer that never looked.
    """
    if not refs:
        return f"Issue: {NO_CLOSING_REF}"
    label = "Issue" if len(refs) == 1 else "Issues"
    return f"{label}: {', '.join(refs)}"
