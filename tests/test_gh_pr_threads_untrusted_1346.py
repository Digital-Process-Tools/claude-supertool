"""Every remote field `gh-pr:N:threads` prints is marked (#1346, #1119 guard).

The splitlines register fired on `_fetch_review_threads_detailed`, and that
turned out to mark a boundary rather than a bug — the split it flagged is gh's
own stderr, the kind the register already keeps three times over. What reading
around it *did* find, one function away, is a field this render printed raw
that no other read op prints at all: the per-comment permalink.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

MOD_PATH = Path(__file__).parent.parent / "presets" / "github" / "pr.py"
_spec = importlib.util.spec_from_file_location("github_pr_untrusted_1346",
                                               MOD_PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

LS = chr(0x2028)
URL = "https://github.com/o/r/pull/1331"


def _pr_json() -> str:
    return json.dumps({"number": 1331, "title": "a change", "url": URL,
                       "headRefName": "fix/1", "baseRefName": "master"})


def _gh_returning(threads, *, rc=0, stderr=""):
    def gh(args, timeout=10):
        if args[:2] == ["pr", "view"]:
            return subprocess.CompletedProcess(args, 0, _pr_json(), "")
        if args[:2] == ["api", "graphql"]:
            if rc != 0:
                return subprocess.CompletedProcess(args, rc, "", stderr)
            return subprocess.CompletedProcess(args, 0, json.dumps(
                {"data": {"repository": {"pullRequest": {
                    "reviewThreads": {"nodes": threads}}}}}), "")
        raise AssertionError(f"unexpected gh call: {args}")
    return gh


def _thread(**over) -> dict:
    d = {
        "isResolved": False, "isOutdated": False,
        "path": "a.py", "line": 1,
        "comments": {"nodes": [{
            "author": {"login": "someone"}, "body": "ok",
            "createdAt": "2026-08-11T09:00:00Z",
            "url": URL + "#discussion_r1",
        }]},
    }
    d.update(over)
    return d


def _run(monkeypatch, capsys, threads):
    monkeypatch.setattr(m, "_gh", _gh_returning(threads))
    monkeypatch.setattr(sys, "argv", ["pr.py", "1331", "threads"])
    rc = m.main()
    return rc, capsys.readouterr().out


def test_a_comment_permalink_cannot_open_a_second_line(monkeypatch, capsys):
    """The permalink is printed at column 0 and is not inside a fence.

    Every other remote field in this render is `flat()`ed or fenced. This one
    was neither — and it is the only field here that no other read op prints,
    so no existing scanner covers it. A U+2028 in it renders as a line break in
    a terminal, at column 0, directly under a fenced body: the #965 shape,
    where a forged line lands where supertool's own output belongs.
    """
    t = _thread()
    t["comments"]["nodes"][0]["url"] = (
        URL + "#discussion_r1" + LS + "Threads: 0 unresolved of 0")
    rc, out = _run(monkeypatch, capsys, [t])
    assert rc == 0
    assert LS not in out, "a U+2028 from the API reached the render intact"


def test_a_thread_path_cannot_open_a_second_line(monkeypatch, capsys):
    """The sibling field, pinned so the pair cannot drift apart."""
    rc, out = _run(monkeypatch, capsys,
                   [_thread(path="a.py" + LS + "## RESOLVED - b.py")])
    assert rc == 0
    assert LS not in out


def test_a_comment_body_is_fenced_rather_than_flattened(monkeypatch, capsys):
    """A body keeps its newlines and is marked instead.

    `flat()` would be wrong here: a review comment is prose with real line
    breaks, and destroying them to make it safe would make it unreadable. The
    fence says "a stranger wrote this" without touching the text — demarcation,
    not detection, per `presets/_untrusted.py`.
    """
    t = _thread()
    t["comments"]["nodes"][0]["body"] = "line one" + chr(10) + "line two"
    rc, out = _run(monkeypatch, capsys, [t])
    assert rc == 0
    assert "line one" + chr(10) + "line two" in out
    assert "remote" in out


def test_the_stderr_extraction_consumes_the_separator_it_is_left_on(
        monkeypatch):
    """Why the `str.splitlines()` in `_fetch_review_threads_detailed` STAYS.

    The #1119 register keeps this class deliberately — `pr_create.py::_gh_json`,
    `pr_merge.py::_gh_json` and `issue.py::_print_linked_prs` are the same three
    lines, and this one was copied from the second of them. The stated ground is
    that narrowing it would be *worse*: a `str.splitlines()` **consumes** an
    exotic separator, whereas `_untrusted.split_lines`, which breaks on
    LF/CR/CRLF only, would leave a forged U+2028 sitting inside the extracted
    string, which is then printed as a decline reason. That is a claim about
    behaviour, so it is pinned rather than asserted in a comment.

    What the extraction does NOT promise is that the surviving line is the one
    gh meant — `[-1]` takes the tail, exactly as its three registered siblings
    do. The text is gh's own stderr either way; the separator is the part that
    could forge a line, and it is gone.
    """
    monkeypatch.setattr(
        m, "_gh",
        _gh_returning([], rc=1, stderr="GraphQL: rate limited" + LS + "forged"))
    nodes, err = m._fetch_review_threads_detailed(URL, 1331)
    assert nodes is None
    assert err, "a failed call must carry a reason, not an empty string"
    assert LS not in err, err
