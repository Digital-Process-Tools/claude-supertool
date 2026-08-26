"""#1965: gh-branch's poller discarded `_repo_identity()`'s own error and
still emitted a state event -- `went_green` included -- off a repo it could
not identify.

`poller._snapshot` already binds two signals it never consulted:
`branch._repo_identity()`'s own error (bound to `_repo_err` in the pre-fix
source) and `scope_for`'s `unresolved`. This file pins the first of the two,
which is the one the issue reproduces end to end: a `gh repo view` failure
must route through the same `LOOKUP_UNAVAILABLE` arm `_head_commit` and
`_run_list` already use, per the poller's own docstring (lines 82-90 at the
time of filing) -- "a `gh` that would not answer at all is
`LOOKUP_UNAVAILABLE`, never `branch.UNKNOWN`" -- and that bar is stricter
than UNKNOWN: it must not reach `branch.verdict()` at all, so it cannot
become GREEN either.

Every must-not-fire case here has a must-fire twin built from the same
fixture, so a harness that answers nothing cannot pass by staying silent.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest import mock

POLLER = Path(__file__).parent.parent / "presets" / "watch" / "sources" / "gh-branch" / "poller.py"
_spec = importlib.util.spec_from_file_location("gh_branch_poller_1965", POLLER)
assert _spec is not None and _spec.loader is not None
poller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poller)

branch = poller.branch


def _proc(returncode=0, stdout="", stderr=""):
    r = mock.Mock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def _fake_gh(repo_view_fails: bool):
    def _call(argv, timeout=20):
        if argv[:2] == ["gh", "api"] and "commits/" in argv[2]:
            return _proc(0, json.dumps({
                "sha": "c391c1333b6793f4fc2e5a2cc830024fd834ffe1",
                "commit": {"committer": {"date": "2026-08-25T12:00:00Z"}},
            }))
        if argv[:3] == ["gh", "run", "list"]:
            # Empty on purpose: NO_RUN is the state a naive fix could still
            # let through, since `scope_for` short-circuits on an empty
            # `selected` and never reaches the workflow-declaration lookup --
            # the repo-identity failure has to be caught before that point,
            # not smuggled in through a side effect of it.
            return _proc(0, "[]")
        if argv[:3] == ["gh", "repo", "view"]:
            if repo_view_fails:
                return _proc(1, "", "HTTP 503: Service Unavailable")
            return _proc(0, json.dumps({"nameWithOwner": "OWNER/REPO",
                                        "defaultBranchRef": {"name": "main"}}))
        raise AssertionError(f"unexpected gh call: {argv}")
    return _call


def _ctx(ref="main"):
    return {"source": "gh-branch", "id": ref, "only": []}


# ---------------------------------------------------------------------------
# _snapshot: a repo-identity failure must not reach verdict()
# ---------------------------------------------------------------------------

def test_a_repo_identity_failure_is_reported_as_an_error_not_a_state() -> None:
    with mock.patch.object(branch, "_gh", side_effect=_fake_gh(repo_view_fails=True)):
        state, sentence, sha, repo, error = poller._snapshot("main")
    assert error, "a gh that could not identify the repository must report an error"
    assert state == "", (
        "a repo-identity failure reached branch.verdict() and produced a "
        f"state anyway: {state!r} ({sentence!r})")


def test_a_working_repo_identity_still_reports_no_run() -> None:
    """The must-fire positive control for the case above, same fixture
    otherwise: this is not a claim that `_snapshot` stopped answering at all."""
    with mock.patch.object(branch, "_gh", side_effect=_fake_gh(repo_view_fails=False)):
        state, sentence, sha, repo, error = poller._snapshot("main")
    assert error == "", error
    assert state == branch.NO_RUN, (state, sentence)
    assert repo == "OWNER/REPO", repo


# ---------------------------------------------------------------------------
# poll(): the error must route through LOOKUP_UNAVAILABLE / branch_unreachable,
# never through went_green (or any other state event)
# ---------------------------------------------------------------------------

def test_poll_reports_branch_unreachable_not_went_green_on_repo_failure() -> None:
    with mock.patch.object(branch, "_gh", side_effect=_fake_gh(repo_view_fails=True)):
        events, new_state = poller.poll({}, _ctx())
    assert len(events) == 1, events
    assert events[0]["event"] == "branch_unreachable", events
    assert new_state["lookup"] == poller.LOOKUP_UNAVAILABLE
    assert not any(e["event"] == "went_green" for e in events)
    assert not any(e["event"] == "unknown" for e in events), (
        "the poller's own docstring: a gh that would not answer at all is "
        "LOOKUP_UNAVAILABLE, never branch.UNKNOWN")


def test_poll_reports_the_found_state_when_repo_identity_works() -> None:
    """The must-fire positive control, same shape as the pair above: fixing
    #1965 must not turn every gh-branch poll into a permanent outage."""
    with mock.patch.object(branch, "_gh", side_effect=_fake_gh(repo_view_fails=False)):
        events, new_state = poller.poll({}, _ctx())
    assert len(events) == 1, events
    assert events[0]["event"] == "no_run", events
    assert new_state["lookup"] == poller.LOOKUP_OK
