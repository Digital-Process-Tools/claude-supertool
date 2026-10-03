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
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, List, Sequence

# Appended, not inserted at 0, and only when absent. `presets/` holds an `xml`
# package, so putting it ahead of the stdlib shadows `xml.etree` for anything
# that later imports it — and this module is imported by nearly every preset,
# which would have made that everyone's problem rather than `xml/query.py`'s.
# The stdlib keeps precedence; `_untrusted` is found because only `presets/`
# has it.
_HERE = str(Path(__file__).parent)
if _HERE not in sys.path:
    sys.path.append(_HERE)

import _untrusted  # noqa: E402  (every state token here is remote text — #1453)

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
    """Uppercase a raw platform state. Empty/None becomes UNKNOWN, not ''.

    Flattened here rather than at the render, because this is the seam every
    render comes through (#1453). `.strip()` alone removes only *leading and
    trailing* whitespace, so an internal newline in a `conclusion`, `status` or
    `state` survived into `summarize()`'s leftover term, into `gh-branch`'s
    two-space-indented table, and into the five other places that print this
    function's return value unchanged — `pr_merge.py`'s `mergeable` and
    `mergeStateStatus` lines, `dashboard.py`'s equivalents, and `branch.py`'s
    run conclusion. Fixing `label()` alone would have closed two of seven,
    which is the per-call-site failure #1449 rejected one file over.

    It cannot change a classification. No member of `PASSED_STATES`,
    `FAILED_STATES`, `PENDING_STATES` or `BENIGN_STATES` contains whitespace,
    so a token that gains a space cannot enter a set and cannot leave one.

    GitHub's conclusions are enums today, which is the reasoning
    `branch.orphan_lines()`'s comment disavows: #851 and #965 were both filed
    after somebody reasoned that way about the field next door.
    """
    s = _untrusted.flat(str(state or "")).strip().upper()
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


# ---------------------------------------------------------------------------
# superseded legs (#1792)
# ---------------------------------------------------------------------------

# The tally term. A leg here has left the passed/failed/pending arithmetic, so
# it needs a name of its own or the sum stops accounting for every leg handed
# in — which is this module's whole reason to exist.
SUPERSEDED_TERM = "superseded"

# Printed under the named disclosure, once. It says what the term means *and*
# what it deliberately does not do, because "we now collapse repeat runs" is
# the reading that would make #1640 a regression.
SUPERSEDED_NOTE = (
    "A later check run of the same name started after each of these finished, "
    "so GitHub decides the check on the later run and these are not counted "
    "red in the tally above. They are also unretractable — no trigger "
    "withdraws a concluded run — which is why they are named here rather than "
    "dropped (#1792). Two runs of one name whose wall clocks overlap supersede "
    "nothing and both still have to pass (#1640). `gh-job:<id>:fail` reads any "
    "of them."
)


def _leg_window(check: object) -> tuple:
    """`(name, started_epoch, completed_epoch)` for one rollup entry.

    Either stamp is `None` when it cannot be established — absent, gh's
    zero-time sentinel, or a `StatusContext`, which carries no run timing at
    all. `parse_ts` already collapses all three to `None`, and that is what
    makes the third state below reachable rather than guessed.
    """
    if not isinstance(check, dict):
        return ("", None, None)
    name = str(check.get("name") or check.get("context") or "?")
    return (name, parse_ts(check.get("startedAt")),
            parse_ts(check.get("completedAt")))


def github_superseded(checks: object) -> List[bool]:
    """Per rollup entry: has a later run of the same check name replaced it?

    Length always `== len(checks)`, so this composes with `github_states()`
    and `github_named_states()` by position.

    **The discriminator is timing, not name, and that is the whole of #1792.**
    GitHub decides a required check on the latest run carrying its name, so a
    pull request the forge calls `clean` used to render `NOT ALL GREEN` here —
    unfixably, because a concluded check run cannot be withdrawn by any trigger
    the maintainer can pull. But collapsing to latest-per-name is not the fix:
    GitHub's default code-scanning setup emits **two runs of one workflow per
    push and both must pass** (#1640), and measured against this repository's
    own `d1bb0837` those two runs emit check runs whose *names collide* — two
    `Analyze (javascript-typescript)`, two `Analyze (python)`, two check
    suites, started in the same second. Latest-per-name drops one of each pair
    and reports a leg that never ran as green.

    So a leg is superseded only when another leg of the same name **started
    strictly after this one completed**. The code-scanning pair overlaps in
    wall clock and neither supersedes anything; the five stale `fragment` runs
    of #1792 completed at 22:23 and the run that passed started eight hours
    later, so all five are superseded.

    This is deliberately **narrower** than GitHub's own rule. Two same-named
    legs that overlap in time with one failed still read red here even where
    the forge says clean. That direction is chosen once and on purpose: a loud
    false alarm costs a reader one look, and a quietly-swallowed failure costs
    a merge.

    Third state, per `docs/validators.md` §"Declining instead of guessing": a
    leg whose completion cannot be read is never superseded. Dropping it would
    be this function manufacturing exactly the silence it exists to remove.
    """
    if not isinstance(checks, list):
        return []
    windows = [_leg_window(c) for c in checks]
    by_name: dict = {}
    for i, (name, _started, _done) in enumerate(windows):
        by_name.setdefault(name, []).append(i)

    out = [False] * len(windows)
    for idxs in by_name.values():
        if len(idxs) < 2:
            continue
        for i in idxs:
            done = windows[i][2]
            if done is None:
                continue
            # `j != i` rather than a per-name maximum: a malformed leg whose
            # own start is after its own completion would otherwise supersede
            # itself and vanish from every count.
            out[i] = any(windows[j][1] is not None and windows[j][1] > done
                         for j in idxs if j != i)
    return out


def github_superseded_count(checks: object) -> int:
    """How many rollup entries a later run of the same name replaced."""
    return sum(1 for flag in github_superseded(checks) if flag)


def github_live_states(checks: object) -> List[str]:
    """`github_states()` with the superseded entries removed.

    What decides green. Never use it as a leg *count*: the reconciliation
    against what the runs declare (#724/#804) is a coverage question and has to
    see every entry, so it keeps reading `github_states()`.
    """
    return [s for s, sup in zip(github_states(checks), github_superseded(checks))
            if not sup]


def github_named_live(checks: object) -> List[tuple]:
    """`github_named_states()` without the superseded entries."""
    return [e for e, sup in zip(github_named_states(checks),
                                github_superseded(checks)) if not sup]


def github_named_superseded(checks: object) -> List[tuple]:
    """`github_named_states()` restricted to the superseded entries."""
    return [e for e, sup in zip(github_named_states(checks),
                                github_superseded(checks)) if sup]


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
    entries: Sequence[tuple[str, str, str, str]], cap: int = NAMED_CAP,
    label_prefix: str = "",
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
    # Flattened here for the same reason `shortfall()` is (#1451): this
    # renders caller-supplied leg names, and a caller that forgot is a new
    # instance of #851 rather than a bug in its own file. `state` needs no
    # flatten because `_label()` goes through `normalize()`, which does.
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for name, state, kind, ident in entries:
        b = bucket(state)
        if b in ("passed", "pending"):
            continue
        label = "failed" if b == "failed" else _label(state)
        groups.setdefault(label, []).append(
            (_untrusted.flat(str(name)), _untrusted.flat(str(kind)),
             _untrusted.flat(str(ident))))

    lines: List[str] = []
    for label in sorted(groups):
        items = groups[label]
        shown = items[:cap]
        parts = [f"{n} ({k} #{i})" if i and k else n for n, k, i in shown]
        text = ", ".join(parts)
        if len(items) > cap:
            text += f", +{len(items) - cap} more"
        lines.append(f"  {label_prefix}{label}: {text}")
    return lines


def superseded_disclosure(entries: Sequence[tuple[str, str, str, str]],
                          cap: int = NAMED_CAP) -> List[str]:
    """Name the legs `summarize()` moved out of the red count (#1792).

    `[]` when nothing was superseded, and never anything else: a leg that
    stopped blocking the merge and left no line behind is the third state
    rendering as the first, which is the defect this whole change is about. The
    grouping is `named_disclosure`'s, prefixed — so `failed`, `cancelled` and
    `timed_out` keep the labels they carry everywhere else and a reader can see
    *what* the superseded leg did, not merely that one existed.
    """
    lines = named_disclosure(entries, cap, label_prefix="superseded ")
    if lines:
        lines.append(f"  {SUPERSEDED_NOTE}")
    return lines


def label(state: str) -> str:
    """Term label for a leftover state. Lowercase, underscores kept.

    Public because a caller rendering a *second* tally next to `summarize()`'s
    must spell its terms identically or the two drift on screen (#803):
    `gh-run` prints `## Failed jobs (6) — 3 failed, 2 cancelled, 1 unknown`
    under a header saying `11 total: … 3 failed … 2 cancelled …`, and the only
    thing making those the same numbers is that both come from here.

    The comma is substituted because `summarize()` promises its line "can be
    audited by arithmetic instead of by trusting the labels", and the terms are
    comma-separated: a state reading `x, 5 passed` renders as `1 x, 5 passed`
    and forges a second term inside the audited list. `normalize()` has already
    taken the newline; this takes the separator, and only here — a comma is
    never legitimate in a state token, while it routinely is in the matrix job
    names `shortfall()` renders (`build (ubuntu-latest, 3.11)`), where the same
    substitution would mangle real data to defend against nothing.
    """
    return normalize(state).lower().replace(",", ";")


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
              cap: int = NAMED_CAP,
              reason: str = "") -> tuple[str, List[str]]:
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
      loud failure for a quiet one. `reason` names *why*, when the caller knows
      (#1181): the decline fired on every PR for an afternoon because the
      caller's own reconciliation budget was one workflow too small, and the
      line said only "could not be established" — true of that, of an
      unreachable API and of a genuinely short tally alike. A warning that
      cannot be told apart from the one that matters is one nobody reads.
    * `declared > found` — a proven shortfall. Both numbers are stated, in
      that order, and the legs that never arrived are named when known.
    * `declared <= found` — reconciled. `<` and not just `==` because a rollup
      legitimately carries checks belonging to no Actions run at all (external
      CI, legacy commit statuses); those are extra, never missing.
    """
    # Caller-supplied text, flattened here rather than trusted to have been
    # flattened by whoever called (#1451). `branch.py` hands these in already
    # flat; `run.py`, `pr.py` and `pr_merge.py` pass leg names straight from
    # the API and a workflow file, and `reason` is built from a child's stderr
    # on one route. A helper that renders remote text is where the convention
    # has to live, or the guarantee reads "flattened if you came through
    # `branch.py`", which is not a guarantee. `flat()` is idempotent, so the
    # caller that already flattened pays a no-op, not a second substitution.
    missing = [_untrusted.flat(str(n)) for n in missing]
    reason = _untrusted.flat(reason)

    if declared is None:
        because = f" ({reason})" if reason else ""
        return (UNVERIFIED_MARK, [
            f"  unverified: {found} {_legs(found)} read, but how many the run declares "
            f"could not be established{because}, so whether these are all of "
            "them is UNKNOWN. Count by hand with "
            "`gh run view <run-id> --json jobs` before treating this as a "
            "merge signal."
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


# gh renders "this timestamp is not set" as a zero time rather than as null —
# `"completedAt": "0001-01-01T00:00:00Z"` on every still-running leg, observed
# on PR #1023 on 2026-08-07. It parses perfectly well, which is the problem:
# an age computed from it is about two thousand years and would be printed
# under a caption saying how long CI has been waiting.
ZERO_TIME_YEAR = 1970


def parse_ts(value: object) -> float | None:
    """An ISO-8601 instant as epoch seconds — `None` when not establishable.

    `None` for every unusable input, and the sentinel above is one of them:
    an unparseable stamp and a zero-time stamp are both "this leg does not
    carry a start time", and neither may become a number.

    `Z` is rewritten rather than passed through because
    `datetime.fromisoformat` only learned to accept it in 3.11 and this repo
    is tested down to 3.9 — a silent `ValueError` there would turn every
    pending age into UNKNOWN on the two oldest legs of the matrix and nowhere
    else, which is the shape of bug a local run cannot see.
    """
    from datetime import datetime, timezone

    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt.year < ZERO_TIME_YEAR:
        return None
    return dt.timestamp()


def pending_disclosure(stamps: Sequence[object],
                       now_epoch: float | None = None) -> tuple[str, List[str]]:
    """`(note, lines)` for how old the pending set is (#801).

    `18 total: 16 passed, 0 failed, 2 pending` is byte-identical whether those
    two legs were queued ninety seconds ago or an hour ago behind a starved
    runner pool. Both readings are consistent with the same line, and only one
    of them means "go look".

    **Which age, and why this one.** Three were available from the payload
    `gh pr view` already returns:

    * *the run's age* — how long CI has been going. Diluted by every leg that
      already finished, so a long-running matrix with a leg that started
      thirty seconds ago reads as alarming and is not.
    * *time since the last leg changed state* — the tempting one, and wrong in
      exactly the case #801 was filed from. Sixteen legs finish, two stay
      queued behind a busy pool: the last state change is as old as the
      sixteenth leg's completion, so a normal queue renders identically to a
      dead run *and renders as dead*. It replaces an ambiguous line with a
      confidently misleading one.
    * *the oldest still-pending leg* — scoped to the thing the reader is
      waiting for and nothing else. This is it.

    None of the three decides queued-from-wedged, and this one does not try:
    #801 is explicit that a `STALLED` threshold would be #750 again, where a
    runner with nothing to do is indistinguishable from a runner that cannot
    work and the inference went 0-for-12. The age is stated; the reader
    compares it against a matrix duration they already know.

    Three states, not two. A pending leg whose start time cannot be
    established is *disclosed*, never dropped from the maximum — dropping it
    makes the reported age **younger**, which is the reassuring direction and
    so the dangerous one. `("", [])` only ever means "there is no pending set".

    **The note is short and lives outside the term list on purpose.**
    `summarize()` promises that every `k <label>` term sums to `N total`, and
    `tests/test_check_tally_454.py` audits exactly that by parsing the terms
    back out of the rendered line. A parenthetical spliced into the pending
    term — `4 pending (oldest 41m; 2 of 4 carry no start time)` — puts prose
    where that parser reads counts: it lost the `pending` term entirely, and a
    stray `2 of 4` before a comma would have been read as a *term* worth 2 and
    quietly corrupted the sum. The check was right and the decoration was
    wrong, so the note goes after the whole tally and anything needing a digit
    or a comma goes on its own line, which is the shape `shortfall()` and
    `named_disclosure()` already use for everything that is not a count.
    """
    if not stamps:
        return ("", [])

    import time

    now = time.time() if now_epoch is None else now_epoch
    ages = [now - ts for ts in (parse_ts(s) for s in stamps) if ts is not None]
    unreadable = len(stamps) - len(ages)
    total = len(stamps)

    if not ages:
        return ("oldest pending age UNKNOWN", [
            f"  pending: no pending {_legs(total)} carries a start time, so "
            "how long the pending set has been outstanding is UNKNOWN. The "
            "PR's Checks tab has the timestamps."
        ])

    note = f"oldest pending {_duration(int(max(ages)))}"
    if unreadable:
        return (note, [
            f"  pending: {unreadable} of {total} pending {_legs(total)} carry "
            "no start time, so the age above is a floor — the true oldest may "
            "be older, and by how much is UNKNOWN."
        ])
    return (note, [])


def github_pending_stamps(checks: object) -> List[object]:
    """`startedAt` for each rollup leg in the pending bucket — one per leg.

    The value is carried through unvalidated, including absent and
    zero-time. `pending_note` is what decides whether it is readable, so a leg
    with no usable stamp still occupies a slot in the list and still gets
    counted in the disclosure. Filtering here is how it would silently
    disappear.
    """
    if not isinstance(checks, list):
        return []
    out: List[object] = []
    for c in checks:
        if not isinstance(c, dict):
            continue
        if bucket(github_state(c)) == "pending":
            out.append(c.get("startedAt"))
    return out


def summarize(states: Sequence[str] | Iterable[str],
              pending_note: str = "", superseded: int = 0) -> str:
    """Render the summary that follows `Checks: `.

    Always opens with `N total` and every count after it sums to N, so the
    line can be audited by arithmetic instead of by trusting the labels::

        12 total: 10 passed, 0 failed, 0 pending, 2 cancelled ⚠ NOT ALL GREEN
        12 total: 0 passed, 0 failed, 12 pending ⚠ NOT ALL GREEN
        3 total: 3 passed, 0 failed, 0 pending
        6 total: 1 passed, 0 failed, 0 pending, 5 superseded
        none reported — no check runs on this commit

    `states` is the **live** set and `superseded` counts the legs a later run
    of the same name replaced (#1792). They are a term rather than a silent
    subtraction for the reason every other term here exists: `N total` has to
    keep accounting for every leg on the commit, and five failures that left
    the red count without leaving a number behind would be exactly the fold
    this module was written to stop. `superseded_disclosure()` names them.

    A superseded leg does not withhold the green — that is #1792's whole
    point — but an **empty** live set with superseded legs does. That is
    unreachable while supersession is defined by an ordering (a chain always
    leaves one live leg at the end), and it is asserted anyway: a tally where
    nothing live decided anything must never render as a pass.

    `pending_note` (#801) is appended after the whole line, past the marker —
    deliberately *outside* the comma-separated term list, because the promise
    above is about the terms and an age is not a count of anything. Splicing
    it into the pending term put prose where `test_check_tally_454`'s parser
    reads counts and broke the audit that is this module's reason to exist.
    It is dropped when nothing is pending, so a settled run cannot grow one::

        18 total: 14 passed, 0 failed, 4 pending ⚠ NOT ALL GREEN — oldest pending 41m
    """
    tokens = [normalize(s) for s in states]
    n_superseded = max(0, int(superseded or 0))
    total = len(tokens) + n_superseded
    if total == 0:
        return NO_CHECKS

    buckets = Counter(bucket(t) for t in tokens)
    n_pending = buckets.get('pending', 0)
    parts = [
        f"{buckets.get('passed', 0)} passed",
        f"{buckets.get('failed', 0)} failed",
        f"{n_pending} pending",
    ]
    leftovers = Counter(_label(t) for t in tokens if bucket(t) == "other")
    for label, count in sorted(leftovers.items(), key=lambda kv: (-kv[1], kv[0])):
        parts.append(f"{count} {label}")
    if n_superseded:
        parts.append(f"{n_superseded} {SUPERSEDED_TERM}")

    line = f"{total} total: " + ", ".join(parts)
    if not tokens or buckets.get("passed", 0) != len(tokens):
        line += f" {NOT_GREEN}"
    if n_pending and pending_note:
        line += f" — {pending_note}"
    return line


def summarize_github(checks: object, with_age: bool = False) -> str:
    """`summarize()` over a raw `statusCheckRollup` list.

    `with_age` opts into #801's pending age. Off by default so the boards that
    render one tally per row stay one line wide; the ops answering about a
    single PR turn it on.
    """
    note = ""
    if with_age:
        note, _lines = pending_disclosure(github_pending_stamps(checks))
    return summarize(github_live_states(checks), note,
                     superseded=github_superseded_count(checks))


def github_pending_lines(checks: object) -> List[str]:
    """The disclosure lines for #801's pending age — `[]` when there is none.

    Separate from `summarize_github` because they are separate outputs: the
    note rides on the tally line, and anything carrying a digit or a comma has
    to go under it rather than inside the term list — see
    `pending_disclosure`.
    """
    _note, lines = pending_disclosure(github_pending_stamps(checks))
    return lines


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


#: `\Z` rather than `$`. The `.strip()` below already removed a trailing
#: newline, so this site was never wrong — it was incidentally saved, which is
#: not the same thing and is not what the next reader will copy (#1188).
_FULL_SHA = re.compile(r"^[0-9a-f]{40}\Z")


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
