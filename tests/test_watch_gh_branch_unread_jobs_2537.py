"""The #2333 direction guard covers a raw-empty run list but not a missing
job list on an otherwise-confirmed commit, so a concluded sha flapped
`went_green` -> `unknown` -> `went_green` on a single `gh run view --json
jobs` miss (#2537).

Observed live: `ac56a9f`, concluded and green since 14:40 UTC, flapped three
times in one afternoon, each dip lasting exactly one poll (35s), each
`unknown` sentence naming a job list ("the job list for `CodeQL` did not come
back") rather than an empty run list. `branch.verdict()` renders a missing job
list straight through as UNKNOWN -- never as `NO_RUN` -- so it never entered
the #2333 guard at all (that guard only widens `NO_RUN`/`NO_RUN_STALE`), and
fired a transition on the very first miss.

`has_unread_jobs` (from `_snapshot`, mirroring `has_failed_leg` from #2355)
is what lets `poll()` tell this UNKNOWN apart from the OTHER way `verdict()`
reaches UNKNOWN -- an unreconciled tally, where every job list did come back
and there is nothing to ride out. Only the job-list-miss UNKNOWN now feeds
the same `no_run_streak` guard and grace window `NO_RUN`/`NO_RUN_STALE`
already use.

The must-fire twins are required by this repository's own defect class
(CLAUDE.md): a "must not fire" assertion (single-poll job-list miss on a
confirmed sha, discarded) passes if the poller emits nothing at all for ANY
reason, so both positive controls the issue itself names live in this same
fixture -- a fresh sha's first miss, and a persistent miss that reaches the
confirm streak.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

POLLER = Path(__file__).parent.parent / "presets" / "watch" / "sources" / "gh-branch" / "poller.py"
_spec = importlib.util.spec_from_file_location("gh_branch_poller_2537", POLLER)
assert _spec is not None and _spec.loader is not None
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)


def _snap(state, sentence="", sha="ac56a9f", repo="", error="", has_failed_leg=False,
          has_unread_jobs=False):
    return (state, sentence, sha, repo, error, has_failed_leg, has_unread_jobs)


def _ctx(ref="master"):
    return {"source": "gh-branch", "id": ref, "only": []}


def _unread_poll(state, sha="ac56a9f"):
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(
                poller.UNKNOWN,
                f"UNKNOWN — the job list for `CodeQL` did not come back on {sha}",
                sha=sha, has_unread_jobs=True)):
        return poller.poll(state, _ctx())


def test_a_single_isolated_job_list_miss_on_a_confirmed_sha_fires_nothing() -> None:
    """The must-not-fire half, and the exact shape of the observed incident:
    one confirmed GREEN sha, one job-list-miss UNKNOWN on that same sha.
    Before this fix this fired `unknown` immediately, because `verdict()`'s
    UNKNOWN reached `poll()` already formed and the #2333 guard only ever
    looked at NO_RUN/NO_RUN_STALE. It must now fire nothing at all, and the
    reported state must stay GREEN, not UNKNOWN, so a consumer polling state
    directly sees no anomaly either."""
    state = {"branch_state": poller.GREEN, "sha": "ac56a9f", "ref": "master",
              "lookup": poller.LOOKUP_OK}
    events, new_state = _unread_poll(state)
    assert events == [], events
    assert new_state["branch_state"] == poller.GREEN, new_state


def test_isolated_job_list_miss_then_recovery_never_touches_the_channel() -> None:
    """The full observed cycle: confirmed GREEN, one job-list miss
    (absorbed), then a clean read again on the same sha. Zero events across
    the whole cycle -- the three `unknown`/`went_green` pairs this issue was
    filed over must both disappear, not just the first half."""
    state = {"branch_state": poller.GREEN, "sha": "ac56a9f", "ref": "master",
              "lookup": poller.LOOKUP_OK}
    events, state = _unread_poll(state)
    assert events == [], events

    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.GREEN,
                                "GREEN — every run on ac56a9f passed")):
        events, state = poller.poll(state, _ctx())
    assert events == [], events
    assert state["branch_state"] == poller.GREEN, state


def test_a_fresh_sha_whose_first_poll_misses_a_job_list_still_fires_unknown() -> None:
    """Positive control #1, named explicitly in the issue's own "Positive
    control" section: a job-list miss on a SHA this poller has never seen
    before is not a flap to ride out -- there is no prior confirmed reading
    to regress from, so it must fire `unknown` for real, on the very first
    poll.

    Weak as evidence for the #2537 widening specifically (self-review
    finding): `sha != prev_sha` alone bypasses the whole guard block via
    `sha_repeated`, the same pre-existing mechanism the #2333 NO_RUN case
    already relies on, so this test passes even with `raw_is_unread_jobs`
    entirely disabled -- it is required by the issue's own contract, not
    proof the widening fires correctly. `test_a_single_isolated_job_list_
    miss_on_a_confirmed_sha_fires_nothing` and `test_persistent_job_list_
    miss_surfaces_unknown_exactly_once`, both keyed on a REPEATED sha, are
    what actually exercise `raw_is_unread_jobs`."""
    state = {"branch_state": poller.GREEN, "sha": "deadbeef", "ref": "master",
              "lookup": poller.LOOKUP_OK}
    events, new_state = _unread_poll(state, sha="fresh123")
    assert len(events) == 1, events
    assert events[0]["event"] == "unknown", events[0]["event"]
    assert new_state["branch_state"] == poller.UNKNOWN, new_state


def test_persistent_job_list_miss_surfaces_unknown_exactly_once() -> None:
    """Positive control #2, named explicitly in the issue: a SHA that misses
    the job list on `UNKNOWN_CONFIRM_STREAK` consecutive polls -- no
    recovery in between -- must surface `unknown`, and stop there: a THIRD
    consecutive miss must not re-announce the same finding (mirroring the
    once-per-anomaly promise #2436 established for the raw-empty case), and
    the sentence at the threshold must name the job list, not an empty run
    list, so a reader is not sent to re-run the wrong diagnostic."""
    state = {"branch_state": poller.GREEN, "sha": "ac56a9f", "ref": "master",
              "lookup": poller.LOOKUP_OK}
    events, state = _unread_poll(state)
    assert events == [], events

    events, state = _unread_poll(state)
    assert len(events) == 1, events
    assert events[0]["event"] == "unknown", events[0]["event"]
    assert state["branch_state"] == poller.UNKNOWN, state
    assert "job list" in events[0]["payload"]["sentence"]
    assert "empty" not in events[0]["payload"]["sentence"]

    events, state = _unread_poll(state)
    assert events == [], events
    assert state["branch_state"] == poller.UNKNOWN, state


def test_a_mixed_streak_sentence_does_not_overclaim_a_uniform_cause() -> None:
    """Self-review finding (#2537): `raw_needs_guard` folds an empty run
    list (`NO_RUN`) and a missing job list (`UNKNOWN` + `has_unread_jobs`)
    into ONE streak, so the two consecutive polls that cross the threshold
    need not share a cause. A first cut of this fix worded the threshold
    sentence as "the last N fetches ... came back empty" / "... did not come
    back", which overclaims when the streak is mixed: this reproduces one
    NO_RUN poll followed by one job-list-miss poll on the same confirmed
    sha, and checks the emitted sentence does not assert every one of the
    two polls failed the same way."""
    state = {"branch_state": poller.GREEN, "sha": "ac56a9f", "ref": "master",
              "lookup": poller.LOOKUP_OK}
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(poller.NO_RUN,
                                "NO RUN — zero workflow runs on ac56a9f",
                                sha="ac56a9f")):
        events, state = poller.poll(state, _ctx())
    assert events == [], events

    events, state = _unread_poll(state)
    assert len(events) == 1, events
    sentence = events[0]["payload"]["sentence"]
    assert "the last 2 fetches" not in sentence, sentence
    assert "came back empty" not in sentence, sentence
    assert "did not come back on the last" not in sentence, sentence
    assert "job list" in sentence, sentence


def test_an_unreconciled_unknown_never_enters_the_guard() -> None:
    """The other UNKNOWN cause `verdict()` has -- every job list DID come
    back, but the tally could not be squared with what the runs declare
    (#837) -- must never be discarded by this guard: `has_unread_jobs` is
    `False` for it, so a genuinely unreconciled read on a repeated,
    previously-confirmed sha still reaches the channel on the first poll,
    exactly as it did before this fix."""
    state = {"branch_state": poller.GREEN, "sha": "ac56a9f", "ref": "master",
              "lookup": poller.LOOKUP_OK}
    with mock.patch.object(
            poller, "_snapshot",
            return_value=_snap(
                poller.UNKNOWN,
                "UNKNOWN — every leg read passed but the tally could not be "
                "squared with what the runs declare",
                sha="ac56a9f", has_unread_jobs=False)):
        events, new_state = poller.poll(state, _ctx())
    assert len(events) == 1, events
    assert events[0]["event"] == "unknown", events[0]["event"]
    assert new_state["branch_state"] == poller.UNKNOWN, new_state
