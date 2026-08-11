"""`gh-pr` refuses a mode word it does not have, and reads nothing first (#1346).

`gh-pr:1331:notamode` used to print the default dashboard at exit 0. So did
`:threads`, `:reviews` and `:comments` — and the default view's own header
prints `Unresolved threads: 1 / 1`, so the op advertised a thing, accepted a
request for it, and returned something else. An agent that hit this concluded
the data was not reachable through supertool and fell back to `gh api`.

The bar every test here holds to: the assertion is the **refusal and its exit
code**, never the absence of a string. A test that only checked the dashboard
had not been printed would pass against an op that crashed.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

MOD_PATH = Path(__file__).parent.parent / "presets" / "github" / "pr.py"
_spec = importlib.util.spec_from_file_location("github_pr_modes_1346", MOD_PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


class _NoGh:
    """Every outward call is a failure of the test, not a fixture.

    A refusal that happens *after* the fetch still costs the round trip and
    still leaves a window where the op renders something. "Nothing was read" is
    part of the claim, so it is asserted structurally rather than in prose.
    """

    def __init__(self):
        self.calls: list = []

    def __call__(self, args, timeout=10):
        self.calls.append(list(args))
        raise AssertionError(f"gh was called on a refusal path: {args}")


REFUSED = ["notamode", "threadss", "reviews", "comments", "STATUS", "diffs"]


@pytest.mark.parametrize("mode", REFUSED)
def test_an_unrecognised_mode_is_refused_and_nothing_is_read(
        monkeypatch, capsys, mode):
    gh = _NoGh()
    monkeypatch.setattr(m, "_gh", gh)
    monkeypatch.setattr(sys, "argv", ["pr.py", "1331", mode])

    rc = m.main()

    out = capsys.readouterr().out
    assert rc == 1, f"{mode!r} exited {rc}, so the caller reads it as answered"
    assert repr(mode) in out
    assert "does not have" in out
    assert gh.calls == []
    # The modes that do exist are named, or the refusal sends the reader
    # hunting through docs for a spelling the op could have printed.
    for known in ("status", "full", "diff", "threads"):
        assert known in out


ACCEPTED = ["status", "full", "diff", "threads", ""]


@pytest.mark.parametrize("mode", ACCEPTED)
def test_a_recognised_mode_is_not_refused(mode):
    """The refusal is scoped to words the op does not have.

    Driven through `_mode_refusal` rather than `main`, because the accepting
    path then goes on to make real calls; what is under test is the gate.
    """
    flags = [mode] if mode else []
    assert m._mode_refusal(flags) == ""


def test_diff_takes_a_path_after_it_and_the_path_is_not_a_mode():
    """`gh-pr:N:diff:presets/github/pr.py` — the token after `diff` is a path.

    Validating it as a mode word would refuse every path-scoped diff, which is
    the failure mode of a guard added without reading what follows it.
    """
    assert m._mode_refusal(["diff", "presets/github/pr.py"]) == ""
    assert m._mode_refusal(["diff", "notamode"]) == ""


def test_the_refusal_survives_a_second_unrecognised_token(monkeypatch, capsys):
    gh = _NoGh()
    monkeypatch.setattr(m, "_gh", gh)
    monkeypatch.setattr(sys, "argv", ["pr.py", "1331", "full", "notamode"])
    rc = m.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "'notamode'" in out
    assert gh.calls == []


# ---------------------------------------------------------------------------
# :threads — the half the refusal alone does not answer
# ---------------------------------------------------------------------------

URL = "https://github.com/o/r/pull/1331"


def _thread(*, resolved=False, path="presets/github/pr.py", line=42,
            bodies=("please fix this",), outdated=False):
    return {
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": path,
        "line": line,
        "comments": {"nodes": [
            {"author": {"login": "reviewer"}, "body": b,
             "createdAt": "2026-08-10T09:00:00Z",
             "url": URL + "#discussion_r1"} for b in bodies]},
    }


def _json_pr() -> str:
    return json.dumps({
        "number": 1331, "title": "a change", "state": "OPEN",
        "url": URL, "headRefName": "fix/1", "baseRefName": "master",
    })


class _Gh:
    """Routes `pr view` and the GraphQL call; records what was asked."""

    def __init__(self, threads, *, graphql_rc=0):
        self.threads = threads
        self.graphql_rc = graphql_rc
        self.calls: list = []

    def __call__(self, args, timeout=10):
        self.calls.append(list(args))
        if args[:2] == ["pr", "view"]:
            return subprocess.CompletedProcess(args, 0, _json_pr(), "")
        if args[:2] == ["api", "graphql"]:
            if self.graphql_rc != 0:
                return subprocess.CompletedProcess(
                    args, self.graphql_rc, "", "GraphQL: rate limited")
            return subprocess.CompletedProcess(args, 0, json.dumps(
                {"data": {"repository": {"pullRequest": {
                    "reviewThreads": {"nodes": self.threads}}}}}), "")
        raise AssertionError(f"unexpected gh call: {args}")


def _run_threads(monkeypatch, capsys, gh) -> tuple:
    monkeypatch.setattr(m, "_gh", gh)
    monkeypatch.setattr(sys, "argv", ["pr.py", "1331", "threads"])
    rc = m.main()
    return rc, capsys.readouterr().out


def test_threads_prints_the_comment_body_the_header_only_counted(
        monkeypatch, capsys):
    gh = _Gh([_thread(bodies=("this line is the review comment",))])
    rc, out = _run_threads(monkeypatch, capsys, gh)
    assert rc == 0
    assert "this line is the review comment" in out
    assert "presets/github/pr.py" in out
    assert "UNRESOLVED" in out


def test_threads_counts_resolved_and_unresolved_the_same_way_the_header_does(
        monkeypatch, capsys):
    gh = _Gh([_thread(resolved=True), _thread(resolved=False)])
    rc, out = _run_threads(monkeypatch, capsys, gh)
    assert rc == 0
    # Deliberately not `"1 / 2" in out`: the DEFAULT dashboard prints
    # `Unresolved threads: 1 / 2` from the same data, so that assertion passes
    # against an op that ignored the mode entirely — the exact defect #1346 is
    # about, re-created inside its own test.
    assert "1 unresolved of 2" in out
    assert "RESOLVED" in out and "UNRESOLVED" in out


def test_a_pr_with_no_threads_says_none_rather_than_printing_nothing(
        monkeypatch, capsys):
    gh = _Gh([])
    rc, out = _run_threads(monkeypatch, capsys, gh)
    assert rc == 0
    assert "no review threads" in out.lower()


def test_a_graphql_failure_is_declined_not_rendered_as_zero_threads(
        monkeypatch, capsys):
    """The third state.

    `[]` because the call failed and `[]` because there are none are opposite
    answers, and `_fetch_review_threads` returns `[]` for both — which is this
    repo's defect class and is why the default header can go silent on a PR
    that does have threads.
    """
    gh = _Gh([], graphql_rc=1)
    rc, out = _run_threads(monkeypatch, capsys, gh)
    assert rc == 1
    assert "UNKNOWN" in out
    assert "no review threads" not in out.lower()
