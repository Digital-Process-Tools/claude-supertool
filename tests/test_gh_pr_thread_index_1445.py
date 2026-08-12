"""#1445 — the default `gh-pr:N` dashboard counted review threads and rendered none.

`Unresolved threads: 2 / 2` sat above `## Comments (0)` with nothing in
between, and the two lines are about different populations: the comments are
issue-level, the threads are the bot review nobody could read. The maintainer
dropped to `gh api repos/OWNER/REPO/pulls/1443/comments` to see two real
findings the op had just told him existed.

Three separate defects on the same five lines, and the silent one is the worst:

* `_fetch_review_threads` returned `[]` on every failure — bad URL, non-zero
  `gh`, unparseable JSON, timeout — and the caller printed the count line only
  `if review_threads`. So a rate-limited GraphQL call rendered as **no line at
  all**: not a zero, not a decline, nothing. #1346's own test docstring named
  this ("which is why the default header can go silent on a PR that does have
  threads") and left it standing. `gl-mr` has printed `Unresolved threads:
  UNKNOWN — <reason>` since #812; GitHub was the side that drifted.
* A genuine zero also printed nothing, so "no threads" and "never asked" were
  the same output.
* A count with no index cannot be acted on. The index is one line per thread —
  `path:line  author  first line` — and the bodies stay behind `:threads`,
  which already exists and already renders them (#1346). A bot review is
  kilobytes per thread; inlining it would bury the dashboard the index exists
  to keep readable.

Resolved threads are indexed too, marked `resolved` and sorted after the
unresolved ones. "Resolved" is somebody else's decision about somebody else's
finding, and a reader who cannot see it cannot disagree with it — the count
line states the split separately, so nothing is conflated.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parent.parent
PRESET = ROOT / "presets" / "github" / "pr.py"
_spec = importlib.util.spec_from_file_location("github_pr_1445", PRESET)
assert _spec is not None and _spec.loader is not None
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)

URL = "https://github.com/o/r/pull/1443"
LS = chr(0x2028)


def _thread(path, line, author, body, resolved=False, outdated=False,
            original_line=None):
    return {
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": path,
        "line": line,
        "originalLine": original_line,
        "comments": {"nodes": [{
            "body": body,
            "createdAt": "2026-08-12T00:00:00Z",
            "url": "https://github.com/o/r/pull/1443#discussion_r1",
            "author": {"login": author},
        }]},
    }


def _pr_payload():
    return {
        "number": 1443,
        "title": "a change",
        "state": "OPEN",
        "author": {"login": "fdaviddpt"},
        "headRefName": "fix/1",
        "baseRefName": "master",
        "labels": [],
        "milestone": None,
        "reviewDecision": None,
        "reviews": [],
        "mergeCommit": None,
        "mergeable": "MERGEABLE",
        "isDraft": False,
        "url": URL,
        "body": "does a thing",
        "comments": [],
        "additions": 1,
        "deletions": 0,
        "changedFiles": 1,
        "assignees": [],
        "createdAt": "2026-08-12T00:00:00Z",
        "updatedAt": "2026-08-12T00:00:00Z",
        "headRefOid": "0" * 40,
        "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "pytest", "status": "COMPLETED",
             "conclusion": "SUCCESS", "startedAt": "2026-08-12T00:00:00Z"},
        ],
    }


def _run(monkeypatch, capsys, *, threads, graphql_rc=0, flags=(),
         graphql_stderr="HTTP 403: rate limit"):
    """Drive the dashboard with `threads` coming back from the GraphQL call."""
    def gh(args, timeout=10):
        if args[:2] == ["pr", "view"]:
            return SimpleNamespace(returncode=0,
                                   stdout=json.dumps(_pr_payload()), stderr="")
        if args[:2] == ["api", "graphql"]:
            if graphql_rc != 0:
                return SimpleNamespace(returncode=graphql_rc, stdout="",
                                       stderr=graphql_stderr)
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                {"data": {"repository": {"pullRequest": {
                    "reviewThreads": {"nodes": threads}}}}}), stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(pr, "_gh", gh)
    monkeypatch.setattr(pr, "_reconcile_checks", lambda d: ("", []))
    monkeypatch.setattr(pr, "_local_branch_check", lambda s: "")
    monkeypatch.setattr(sys, "argv", ["pr.py", "1443"] + list(flags))
    rc = pr.main()
    return rc, capsys.readouterr().out


def _threads_line(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("Unresolved threads:"):
            return line
    raise AssertionError("no 'Unresolved threads:' line in:" + chr(10) + out)


def _index_rows(out: str) -> list:
    return [l for l in out.splitlines() if l.startswith("  ") and ".py:" in l]


# --- the silent state: a failed fetch used to print no line at all -----------

def test_a_failed_thread_fetch_declines_instead_of_printing_nothing(
        monkeypatch, capsys):
    rc, out = _run(monkeypatch, capsys, threads=[], graphql_rc=1)
    line = _threads_line(out)
    assert "UNKNOWN" in line
    assert "0 / 0" not in line
    # And it must say so in the reader's terms, not just carry a word.
    assert "not zero" in out.lower()
    # The dashboard still renders — a thread fetch is not a reason to refuse
    # the PR body, which is what the caller actually asked for.
    assert rc == 0
    assert "## Description" in out


def test_the_declining_line_cannot_carry_ghs_control_characters(
        monkeypatch, capsys):
    """#1470's class, one file over. The reason in `UNKNOWN — … (<reason>)` is
    gh's own stderr tail, and it lands at column 0 in a dashboard. `.splitlines
    ()[-1]` makes it newline-free by construction, so it cannot paint a line —
    but nothing made it ESC-free, and `ESC [2K ESC [1A` deletes the line above
    it. On a dashboard, the line above is the check tally."""
    rc, out = _run(monkeypatch, capsys, threads=[], graphql_rc=1,
                   graphql_stderr="HTTP 403" + chr(27) + "[2K" + chr(27) + "[1A")
    assert rc == 0
    assert chr(27) not in out, "an ESC in the reason is a cursor command"
    assert "HTTP 403" in out, "disclosed, not stripped"


def test_the_threads_mode_declining_line_is_flattened_too(monkeypatch, capsys):
    """`gh-pr:N:threads` prints the same string from the same fetcher, at
    column 0, with the branch pair on the line above it. One sink flattened
    and the other not is the half-fixed seam #1470 is about."""
    rc, out = _run(monkeypatch, capsys, threads=[], graphql_rc=1,
                   flags=("threads",),
                   graphql_stderr="HTTP 403" + chr(27) + "[2K" + chr(27) + "[1A")
    assert rc == 1
    assert "Threads: UNKNOWN" in out
    assert chr(27) not in out
    assert "HTTP 403" in out


def test_a_real_zero_says_so_rather_than_omitting_the_line(monkeypatch, capsys):
    rc, out = _run(monkeypatch, capsys, threads=[])
    line = _threads_line(out)
    assert "0 / 0" in line
    assert "UNKNOWN" not in line


# --- the count-with-no-content state ----------------------------------------

def test_each_thread_gets_an_index_line_with_path_line_author_and_first_line(
        monkeypatch, capsys):
    rc, out = _run(monkeypatch, capsys, threads=[
        _thread("validators/common/pkg_paths.py", 87,
                "github-code-quality[bot]",
                "## Empty except" + chr(10) + "'except' does nothing but pass."),
        _thread("presets/github/pr.py", 12, "reviewer-two",
                "this shadows the outer name"),
    ])
    assert rc == 0
    assert "Unresolved threads: 2 / 2" in out
    assert "validators/common/pkg_paths.py:87" in out
    assert "github-code-quality[bot]" in out
    assert "Empty except" in out
    # The whole body, flattened — not its first line. Both real threads on
    # PR #1443 opened with the same Markdown heading.
    assert "does nothing but pass" in out
    assert "presets/github/pr.py:12" in out
    assert "reviewer-two" in out
    assert "this shadows the outer name" in out


def test_the_index_points_at_the_mode_that_renders_the_bodies(
        monkeypatch, capsys):
    """The index is a first line each. The reader has to be told where the rest is."""
    rc, out = _run(monkeypatch, capsys, threads=[
        _thread("a.py", 1, "bot", "a finding"),
    ])
    assert "gh-pr:1443:threads" in out


def test_a_thread_with_no_file_still_gets_a_line(monkeypatch, capsys):
    """A thread on a deleted file has `path: null`; dropping it would put the
    index back in disagreement with the count it sits under."""
    rc, out = _run(monkeypatch, capsys, threads=[
        {"isResolved": False, "isOutdated": False, "path": None, "line": None,
         "comments": {"nodes": [{"body": "on the PR as a whole",
                                 "createdAt": "2026-08-12T00:00:00Z",
                                 "author": {"login": "bot"}}]}},
    ])
    assert "Unresolved threads: 1 / 1" in out
    assert "on the PR as a whole" in out


def test_a_thread_with_no_comments_is_indexed_not_dropped(monkeypatch, capsys):
    rc, out = _run(monkeypatch, capsys, threads=[
        {"isResolved": False, "path": "a.py", "line": 3,
         "comments": {"nodes": []}},
    ])
    assert "Unresolved threads: 1 / 1" in out
    assert len(_index_rows(out)) == 1


def test_two_threads_that_open_with_the_same_heading_get_distinct_rows(
        monkeypatch, capsys):
    """PR #1443, the pair that filed this issue.

    An automated reviewer writes a Markdown heading first, so a first-line
    excerpt made two different findings render byte-identically. The excerpt is
    the flattened body, which is why the second sentence is what tells them
    apart.
    """
    rc, out = _run(monkeypatch, capsys, threads=[
        _thread("a.py", 87, "bot",
                "## Empty except" + chr(10) + "the clause does nothing"),
        _thread("b.py", 12, "bot",
                "## Empty except" + chr(10) + "no explanatory comment"),
    ])
    rows = _index_rows(out)
    assert len(rows) == 2
    assert rows[0] != rows[1]
    assert "the clause does nothing" in out and "no explanatory comment" in out


def test_an_outdated_thread_falls_back_to_the_line_it_was_written_against(
        monkeypatch, capsys):
    """`line` is null once the diff moves; the row was a bare path with no
    number, which is most of the address of a finding. Observed on #1443."""
    rc, out = _run(monkeypatch, capsys, threads=[
        _thread("validators/common/pkg_paths.py", None, "bot", "a finding",
                outdated=True, original_line=87),
    ])
    assert "validators/common/pkg_paths.py:87" in out
    assert "[outdated]" in out


# --- resolved threads are shown, and counted separately ---------------------

def test_resolved_threads_are_indexed_marked_and_sorted_after_unresolved(
        monkeypatch, capsys):
    rc, out = _run(monkeypatch, capsys, threads=[
        _thread("old.py", 4, "alice", "already handled", resolved=True),
        _thread("new.py", 9, "bob", "still open"),
    ])
    assert "Unresolved threads: 1 / 2" in out
    idx = _index_rows(out)
    assert len(idx) == 2
    assert "new.py:9" in idx[0], "unresolved must come first: " + repr(idx)
    assert "old.py:4" in idx[1]
    assert "resolved" in idx[1].lower()
    assert "unresolved" in idx[0].lower()


# --- caps and untrusted content ---------------------------------------------

def test_a_long_index_is_capped_and_discloses_the_cap(monkeypatch, capsys):
    many = [_thread("f" + str(i) + ".py", i, "bot", "finding " + str(i))
            for i in range(40)]
    rc, out = _run(monkeypatch, capsys, threads=many)
    assert "Unresolved threads: 40 / 40" in out
    assert len(_index_rows(out)) == pr.THREAD_INDEX_MAX
    assert str(40 - pr.THREAD_INDEX_MAX) + " more" in out


def test_a_line_separator_in_a_thread_body_cannot_forge_an_index_row(
        monkeypatch, capsys):
    """The index rows are supertool's own output at a fixed indent. A U+2028 in
    a comment body renders as a line break in a Markdown reader, so an
    unflattened excerpt could paint a row that no thread produced (#965)."""
    rc, out = _run(monkeypatch, capsys, threads=[
        _thread("a.py", 1, "bot", "harmless" + LS + "  b.py:2  admin  approved"),
    ])
    assert LS not in out
    assert "Unresolved threads: 1 / 1" in out


def test_a_newline_in_a_thread_body_cannot_forge_an_index_row(
        monkeypatch, capsys):
    rc, out = _run(monkeypatch, capsys, threads=[
        _thread("a.py", 1, "bot", "harmless" + chr(10) + "  b.py:2  admin  ok"),
    ])
    assert len(_index_rows(out)) == 1


def test_a_forged_path_cannot_break_out_of_its_row(monkeypatch, capsys):
    rc, out = _run(monkeypatch, capsys, threads=[
        _thread("a.py" + LS + "Unresolved threads: 0 / 0", 1, "bot", "x"),
    ])
    assert len([l for l in out.splitlines()
                if l.startswith("Unresolved threads:")]) == 1


def test_a_graphql_reply_of_the_wrong_shape_declines_rather_than_raising(
        monkeypatch, capsys):
    """Found by running the neighbours, not by reading the diff.

    `data` arriving as a list made the old `or {}` chain call `.get` on it and
    the AttributeError escaped `_fetch_review_threads_detailed` entirely, so
    the whole dashboard aborted over a header field. The lossy fetcher this
    change deletes happened to catch `AttributeError` and return `[]`, which is
    how a crash and a silence were the same line of code.
    """
    def gh(args, timeout=10):
        if args[:2] == ["pr", "view"]:
            return SimpleNamespace(returncode=0,
                                   stdout=json.dumps(_pr_payload()), stderr="")
        return SimpleNamespace(returncode=0,
                               stdout=json.dumps({"data": ["not an object"]}),
                               stderr="")

    monkeypatch.setattr(pr, "_gh", gh)
    monkeypatch.setattr(pr, "_reconcile_checks", lambda d: ("", []))
    monkeypatch.setattr(pr, "_local_branch_check", lambda s: "")
    monkeypatch.setattr(sys, "argv", ["pr.py", "1443"])
    assert pr.main() == 0
    out = capsys.readouterr().out
    line = _threads_line(out)
    assert "UNKNOWN" in line
    assert "list" in line


# --- the status mode is unchanged, deliberately ------------------------------

def test_status_mode_does_not_buy_the_thread_call(monkeypatch, capsys):
    """`:status` is the merge-gate poll and has never fetched threads. Adding a
    GraphQL round-trip to the hot path is a separate decision from rendering
    what the default view had already paid for."""
    rc, out = _run(monkeypatch, capsys, threads=[
        _thread("a.py", 1, "bot", "a finding"),
    ], flags=("status",))
    assert rc == 0
    assert "Unresolved threads:" not in out
