"""#1491 — `gh-pr` printed a GraphQL page cap as if it were the PR's total.

`_THREADS_QUERY` selects `reviewThreads(first: 100)` and `comments(first: 50)`
and requests no `pageInfo`, so the reply cannot say whether it saw everything.
Two renders then divided by what it got:

* `_thread_index` — `Unresolved threads: {unresolved} / {len(threads)}`
* `_render_threads` — `Threads: {unresolved} unresolved of {len(threads)}`

At the cap those read as facts about the pull request and are facts about the
fetch. `… N more not indexed here` is computed off the same truncated set, so it
under-reports by the same amount, and the per-thread comment list simply stopped
at 50 with nothing said.

The house defect (`docs/validators.md`, "Declining instead of guessing"): an
absence produced by the tool read as an absence in the world. The line directly
above it already says `Unresolved threads: 0 / 0 — read, not assumed.`, which is
a claim about honesty sitting next to the number that was not making it.

**A reply holding exactly the cap and a PR holding exactly the cap are the same
bytes**, so the render's claim is bounded either way: `at least N` is true in
both, and it is the only thing the reply supports. Nothing here asserts more
pages exist — it stops asserting they do not.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

PRESET = Path(__file__).resolve().parent.parent / "presets" / "github" / "pr.py"
_spec = importlib.util.spec_from_file_location("github_pr_1491", PRESET)
assert _spec is not None and _spec.loader is not None
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)

URL = "https://github.com/o/r/pull/1491"


def _thread(n: int, resolved: bool = False, comments: int = 1) -> dict:
    return {
        "isResolved": resolved,
        "isOutdated": False,
        "path": f"f{n}.py",
        "line": n,
        "originalLine": None,
        "comments": {"nodes": [{
            "body": f"finding {n} comment {c}",
            "createdAt": "2026-08-12T00:00:00Z",
            "url": f"{URL}#c{n}-{c}",
            "author": {"login": "bot"},
        } for c in range(comments)]},
    }


def _index(threads: list) -> str:
    return chr(10).join(pr._thread_index(threads, "", 1491))


def _threads_view(monkeypatch, capsys, threads: list) -> str:
    def gh(argv, **kw):
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "number": 1491, "title": "t", "url": URL,
            "headRefName": "h", "baseRefName": "master"}), stderr="")

    monkeypatch.setattr(pr, "_gh", gh)
    monkeypatch.setattr(pr, "_fetch_review_threads_detailed",
                        lambda url, n: (threads, ""))
    pr._render_threads("1491", None)
    return capsys.readouterr().out


# --- the cap must not be printed as a denominator ---------------------------

def test_index_at_the_page_cap_states_a_floor_not_a_total() -> None:
    threads = [_thread(i, resolved=i % 2 == 0)
               for i in range(pr.THREADS_PAGE_MAX)]
    line = _index(threads).splitlines()[0]
    # The exact shape that was wrong was a bare `50 / 100`. Asserted as a prefix
    # rather than as an absence, so the disclosure clause that follows — which
    # names the cap itself — cannot make the assertion pass on its own.
    assert line.startswith(
        f"Unresolved threads: at least 50 / at least {pr.THREADS_PAGE_MAX}"), line


def test_index_below_the_page_cap_is_an_unqualified_count() -> None:
    """The regression guard. Qualifying a number the reply *does* establish
    would be the same defect pointed the other way — a reader who cannot tell
    `0 / 0` from `0 / at least 0` learns nothing from either."""
    line = _index([_thread(1), _thread(2, resolved=True)]).splitlines()[0]
    assert line == "Unresolved threads: 1 / 2", line
    assert "at least" not in line


def test_withheld_index_rows_are_a_floor_at_the_cap() -> None:
    threads = [_thread(i) for i in range(pr.THREADS_PAGE_MAX)]
    rows = [l for l in _index(threads).splitlines()
            if "not indexed here" in l]
    assert len(rows) == 1, rows
    assert "at least" in rows[0], rows[0]


def test_withheld_index_rows_below_the_cap_are_exact() -> None:
    threads = [_thread(i) for i in range(pr.THREAD_INDEX_MAX + 3)]
    rows = [l for l in _index(threads).splitlines()
            if "not indexed here" in l]
    assert len(rows) == 1, rows
    assert "at least" not in rows[0], rows[0]
    assert "3 more" in rows[0], rows[0]


def test_threads_view_at_the_page_cap_states_a_floor(monkeypatch, capsys) -> None:
    threads = [_thread(i, resolved=i % 2 == 0)
               for i in range(pr.THREADS_PAGE_MAX)]
    out = _threads_view(monkeypatch, capsys, threads)
    line = next(l for l in out.splitlines() if l.startswith("Threads: "))
    # The bare shape that was wrong, matched at the head of the line so the
    # disclosure clause's own "first page of 100" cannot satisfy it.
    assert line.startswith(
        f"Threads: at least 50 unresolved of at least {pr.THREADS_PAGE_MAX}"), line


def test_threads_view_below_the_page_cap_is_unqualified(
        monkeypatch, capsys) -> None:
    out = _threads_view(monkeypatch, capsys,
                        [_thread(1), _thread(2, resolved=True)])
    line = next(l for l in out.splitlines() if l.startswith("Threads: "))
    assert line == "Threads: 1 unresolved of 2", line


# --- the comment cap inside one thread was silent entirely ------------------

def test_a_thread_at_the_comment_cap_says_the_list_was_cut(
        monkeypatch, capsys) -> None:
    out = _threads_view(monkeypatch, capsys,
                        [_thread(1, comments=pr.COMMENTS_PAGE_MAX)])
    assert str(pr.COMMENTS_PAGE_MAX) in out
    assert "the fetch stops there" in out, out


def test_a_thread_below_the_comment_cap_says_nothing(
        monkeypatch, capsys) -> None:
    out = _threads_view(monkeypatch, capsys, [_thread(1, comments=2)])
    assert "the fetch stops there" not in out, out


# --- the caps the renders reason about must be the caps the query asks for ---

def test_the_query_asks_for_exactly_the_caps_the_renders_compare_against() -> None:
    """The 100 was a literal inside the query string and a hand-copied number in
    a comment above it. A render that infers truncation from `len(threads)`
    against a constant is only sound while the two agree, and nothing made them.
    """
    assert f"reviewThreads(first:{pr.THREADS_PAGE_MAX})" in pr._THREADS_QUERY
    assert f"comments(first:{pr.COMMENTS_PAGE_MAX})" in pr._THREADS_QUERY
