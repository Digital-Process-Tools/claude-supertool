"""Tests for the github-issue-feed watch source (issue #525).

#525's body asked for a per-id `watch:github-issue:<n>` poller; its one comment
argued that a per-id poller cannot answer the question that motivated it — a
label-triggered workflow fires on issues the poller was never spawned for, and
"was an issue created?" is unanswerable by construction from a watcher over one
known number. This source is the answer to the comment: it polls the
*population*, and every fact #525 named (labels, assignees, comments, closure)
is already carried by the rows of that one call, so no per-issue fan-out exists
to be blind.

The tests therefore pin what the discovery gap turns on:

- a number that was not in the population last poll is announced, and is
  classified as *opened* only when the feed can establish it did not exist at
  the previous look — never as a default;
- a label or assignee moving is reported as the delta, not as "something
  changed", because the count sends the reader back to the API for the one fact
  they needed;
- REST /issues returns pull requests in the same array as issues, so a PR must
  never enter the population;
- an unestablished population — a failed call, a truncated page run, an
  unknown filter token — must never read as "every issue you had is gone", and
  must not be silent either.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SOURCE_DIR = REPO / "presets" / "watch" / "sources" / "github-issue-feed"

_spec = importlib.util.spec_from_file_location("gh_issue_feed_poller",
                                               SOURCE_DIR / "poller.py")
assert _spec is not None and _spec.loader is not None
feed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(feed)

CTX = {"source": "github-issue-feed", "id": "@open", "only": []}


def _row(num, *, labels=(), assignees=(), comments=0,
         created_at="2026-01-01T00:00:00Z", state_reason=""):
    return {
        "title": f"title {num}",
        "url": f"https://github.com/o/r/issues/{num}",
        "labels": list(labels),
        "assignees": list(assignees),
        "comments": comments,
        "created_at": created_at,
        "state_reason": state_reason,
    }


def _pop(*nums, **kw):
    return {str(n): _row(n, **kw) for n in nums}


def _keys(events):
    return [e["event"] for e in events]


def _by_key(events, key):
    return next(e for e in events if e["event"] == key)


@pytest.fixture
def rig(monkeypatch):
    calls = {"looked_up": []}

    def _lookup(number):
        calls["looked_up"].append(number)
        return calls.get("states", {}).get(number, "")

    monkeypatch.setattr(feed, "lookup_issue_state", _lookup)
    return {"calls": calls, "monkeypatch": monkeypatch}


def _population(rig, *pops, error=""):
    seq = list(pops)

    def _fetch(_scope):
        pop = seq.pop(0) if len(seq) > 1 else seq[0]
        return (pop, error if pop is None else "")

    rig["monkeypatch"].setattr(feed, "fetch_population", _fetch)


def _run(rig, *pops, state=None, error=""):
    """Poll once per queued population, returning (events, state) per poll."""
    _population(rig, *pops, error=error)
    out = []
    st = {} if state is None else state
    for _ in pops:
        events, st = feed.poll(st, CTX)
        out.append((events, st))
    return out


# ---------------------------------------------------------------------------
# scope — the caller picks the population, and an unreadable one is not empty
# ---------------------------------------------------------------------------

def test_default_scope_is_every_open_issue():
    assert feed.resolve_filters("@open") == {"state": "open"}


def test_repeated_label_tokens_become_one_rest_labels_param():
    """REST takes `labels` comma-joined; the scope separator is also a comma,
    so a repeated key is the only unambiguous spelling of "and"."""
    assert feed.resolve_filters("state=open,label=lane-watch,label=cohort-1") == {
        "state": "open", "labels": "lane-watch,cohort-1"}


def test_unknown_filter_token_refuses_the_whole_scope():
    """A token this source cannot apply describes a *wider* population than was
    asked for, so building it anyway would announce strangers' issues (#939's
    reasoning, on the GitHub side)."""
    assert feed.resolve_filters("state=open,mystery=1") is None


# ---------------------------------------------------------------------------
# baseline and discovery
# ---------------------------------------------------------------------------

def test_first_poll_is_silent_but_records_the_population(rig):
    (events, state), = _run(rig, _pop(1, 2, 3))
    assert events == []
    assert sorted(state["known"]) == ["1", "2", "3"]
    assert state["observed_at"]


def test_new_number_created_since_the_last_look_is_announced_as_opened(rig):
    first = _pop(1)
    second = dict(first)
    second["9"] = _row(9, created_at="2099-01-01T00:00:00Z")
    (_, _), (events, _) = _run(rig, first, second)
    assert _keys(events) == ["issue_opened"]
    assert _by_key(events, "issue_opened")["payload"]["number"] == "9"


def test_a_number_that_predates_the_last_look_is_not_called_opened(rig):
    """It entered the *filter* — reopened, relabelled in, assigned in. Calling
    that "opened" is a claim the population query cannot support."""
    first = _pop(1)
    second = dict(first)
    second["9"] = _row(9, created_at="2000-01-01T00:00:00Z")
    (_, _), (events, _) = _run(rig, first, second)
    assert _keys(events) == ["issue_entered_feed"]


def test_a_number_the_feed_itself_saw_close_comes_back_as_reopened(rig):
    """The one case the feed can establish rather than infer."""
    rig["calls"]["states"] = {"1": "closed"}
    a, b, c = _pop(1, 2), _pop(2), _pop(1, 2)
    (_, _), (closed, _), (events, _) = _run(rig, a, b, c)
    assert _keys(closed) == ["issue_closed"]
    assert _keys(events) == ["issue_reopened"]


# ---------------------------------------------------------------------------
# the label case — #525 names it as the strongest, and asks for the delta
# ---------------------------------------------------------------------------

def test_label_added_reports_which_label(rig):
    first = {"1": _row(1, labels=["blocked"])}
    second = {"1": _row(1, labels=["blocked", "jimmy-help-needed"])}
    (_, _), (events, _) = _run(rig, first, second)
    assert _keys(events) == ["issue_labeled"]
    payload = _by_key(events, "issue_labeled")["payload"]
    assert payload["added"] == "jimmy-help-needed"
    assert payload["changed"] == "+jimmy-help-needed"
    assert payload["labels"] == "blocked,jimmy-help-needed"


def test_label_removed_reports_which_label(rig):
    first = {"1": _row(1, labels=["blocked", "keep"])}
    second = {"1": _row(1, labels=["keep"])}
    (_, _), (events, _) = _run(rig, first, second)
    assert _keys(events) == ["issue_unlabeled"]
    assert _by_key(events, "issue_unlabeled")["payload"]["changed"] == "-blocked"


def test_one_label_swapped_for_another_reports_both_sides(rig):
    first = {"1": _row(1, labels=["blocked"])}
    second = {"1": _row(1, labels=["ready"])}
    (_, _), (events, _) = _run(rig, first, second)
    assert set(_keys(events)) == {"issue_labeled", "issue_unlabeled"}


def test_label_reordering_is_not_a_change(rig):
    """GitHub does not promise an order. A set that reordered has not moved,
    and an event per poll is how a reader learns to skim the board."""
    first = {"1": _row(1, labels=["a", "b"])}
    second = {"1": _row(1, labels=["b", "a"])}
    (_, _), (events, _) = _run(rig, first, second)
    assert events == []


def test_assignee_change_reports_the_login(rig):
    first = {"1": _row(1, assignees=[])}
    second = {"1": _row(1, assignees=["fdaviddpt"])}
    (_, _), (events, _) = _run(rig, first, second)
    assert _keys(events) == ["issue_assigned"]
    assert _by_key(events, "issue_assigned")["payload"]["added"] == "fdaviddpt"


def test_comment_count_rising_fires_once_with_the_delta(rig):
    first = {"1": _row(1, comments=2)}
    second = {"1": _row(1, comments=5)}
    (_, _), (events, _) = _run(rig, first, second)
    assert _keys(events) == ["issue_comment_added"]
    assert _by_key(events, "issue_comment_added")["payload"]["new_count"] == 3


def test_comment_count_falling_is_not_an_event(rig):
    """A deleted comment lowers the count. Nothing arrived."""
    first = {"1": _row(1, comments=5)}
    second = {"1": _row(1, comments=2)}
    (_, _), (events, _) = _run(rig, first, second)
    assert events == []


# ---------------------------------------------------------------------------
# departures — vanished is not closed
# ---------------------------------------------------------------------------

def test_vanished_number_confirmed_closed_is_reported_closed(rig):
    rig["calls"]["states"] = {"7": "closed"}
    (_, _), (events, _) = _run(rig, _pop(7, 8), _pop(8))
    assert _keys(events) == ["issue_closed"]
    assert rig["calls"]["looked_up"] == ["7"]


def test_vanished_number_still_open_left_the_filter_not_the_world(rig):
    rig["calls"]["states"] = {"7": "open"}
    (_, _), (events, _) = _run(rig, _pop(7, 8), _pop(8))
    assert _keys(events) == ["issue_left_feed"]
    assert _by_key(events, "issue_left_feed")["payload"]["issue_state"] == "open"


def test_vanished_number_that_cannot_be_looked_up_says_unknown(rig):
    rig["calls"]["states"] = {}
    (_, _), (events, _) = _run(rig, _pop(7), _pop())
    assert _keys(events) == ["issue_left_feed"]
    assert _by_key(events, "issue_left_feed")["payload"]["issue_state"] == "unknown"


# ---------------------------------------------------------------------------
# the absence defect — an unestablished population is not an empty one
# ---------------------------------------------------------------------------

def test_failed_fetch_keeps_the_population_and_fires_no_departures(rig):
    (_, first_state), (events, state) = _run(rig, _pop(1, 2), None,
                                             error="ERROR: gh timed out")
    assert "issue_closed" not in _keys(events)
    assert "issue_left_feed" not in _keys(events)
    assert state["known"] == first_state["known"]


def test_failed_fetch_says_so_once_not_every_poll(rig):
    (_, _), (first, _), (second, _) = _run(rig, _pop(1), None, None,
                                           error="ERROR: gh timed out")
    assert _keys(first) == ["issues_unreachable"]
    assert _by_key(first, "issues_unreachable")["payload"]["error"] == "ERROR: gh timed out"
    assert second == []


def test_recovery_after_an_outage_does_not_re_announce_the_population(rig):
    """The comparison fields have to survive the outage untouched, or the
    transition that happened while we were blind is lost or announced twice."""
    (_, _), (_, _), (events, _) = _run(rig, _pop(1, 2), None, _pop(1, 2),
                                       error="ERROR: gh timed out")
    assert events == []


def test_is_terminal_is_never_true(rig):
    """A population has no final state. A feed that ends is the blindness back."""
    assert feed.is_terminal({}) is False
    assert feed.is_terminal({"known": {}, "lookup": "ok"}) is False


# ---------------------------------------------------------------------------
# fetch_population — the parts that talk to gh
# ---------------------------------------------------------------------------

def _fake_gh(monkeypatch, pages, returncode=0, stderr=""):
    seen = []

    def _run_gh(args, timeout=10):
        seen.append(args)
        body = pages[len(seen) - 1] if len(seen) <= len(pages) else []
        return subprocess.CompletedProcess(
            args, returncode, json.dumps(body), stderr)

    monkeypatch.setattr(feed, "_gh", _run_gh)
    return seen


def test_pull_requests_never_enter_the_population(monkeypatch):
    """REST /repos/O/R/issues returns PRs in the same array. 2 of 79 rows on
    this repository were PRs at the time of writing, and a PR entering the
    population fires issue_opened for every PR anyone raises."""
    _fake_gh(monkeypatch, [[
        {"number": 5, "title": "an issue", "html_url": "u", "labels": [],
         "assignees": [], "comments": 0, "created_at": "2026-01-01T00:00:00Z"},
        {"number": 6, "title": "a PR", "html_url": "u", "labels": [],
         "assignees": [], "comments": 0, "created_at": "2026-01-01T00:00:00Z",
         "pull_request": {"url": "..."}},
    ]])
    pop, error = feed.fetch_population("@open")
    assert error == ""
    assert list(pop) == ["5"]


def test_a_truncated_page_run_is_an_unestablished_population_not_a_short_one(monkeypatch):
    """Stopping at the page cap and returning what fits would fire a departure
    for every issue past it — the tool's own absence read as the world's."""
    full = [{"number": n, "title": "t", "html_url": "u", "labels": [],
             "assignees": [], "comments": 0,
             "created_at": "2026-01-01T00:00:00Z"}
            for n in range(feed.PER_PAGE)]
    _fake_gh(monkeypatch, [full] * (feed.MAX_PAGES + 1))
    pop, error = feed.fetch_population("@open")
    assert pop is None
    assert "more than" in error


def test_gh_failure_carries_the_reason_rather_than_collapsing_to_none(monkeypatch):
    _fake_gh(monkeypatch, [[]], returncode=1, stderr="HTTP 401: Bad credentials")
    pop, error = feed.fetch_population("@open")
    assert pop is None
    assert "gh auth login" in error


def test_an_unknown_scope_token_is_reported_as_a_reason_not_a_silence(monkeypatch):
    _fake_gh(monkeypatch, [[]])
    pop, error = feed.fetch_population("state=open,mystery=1")
    assert pop is None
    assert "mystery" in error


def test_events_json_lists_exactly_what_the_poller_can_emit():
    """A source's declared vocabulary is what `watches` and the docs read. An
    event key that no branch emits is a claim that is simply untrue, and one
    the poller emits but does not declare cannot be filtered with `only=`."""
    declared = {e["key"] for e in json.loads(
        (SOURCE_DIR / "events.json").read_text(encoding="utf-8"))["events"]}
    emitted = set(feed.EVENT_KEYS)
    assert declared == emitted
