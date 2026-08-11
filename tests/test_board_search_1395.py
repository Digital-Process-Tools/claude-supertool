"""Server-side search on `gh-issues` and `gl-mrs` (#1395).

`gh issue list` and `glab mr list` are both mapped under `replaces`, so the
shipped guard refuses them and points the caller at the op. Neither op could
search, so the question the raw commands answer — *find the issue whose text
mentions X, server-side* — had no route at all. `per=` and `enrich_cap` widen
the population; they do not filter it.

**The judgment this file pins is the spelling.** One key, `search=`, on both
families. The two engines are genuinely different — GitHub's `--search` is a
query language, GitLab's is a substring `search=` parameter — and the
temptation is to conclude that a shared spelling is a lie. It is not, for a
mechanical reason: the board grammar is ONE comma-separated segment and
supertool splits an op argument on `:`, so a value carrying `in:title` is
refused by `extra_segments_error` before any filter is parsed. GitHub's
qualifier language is unreachable from here by construction. Stripped of
qualifiers both engines answer "this text occurs in this thing", and what
survives the difference is **scope**, not syntax:

* GitHub searches title, body and comments.
* GitLab searches title and description only — `glab mr list --help` says
  "Filter by <string> in title and description".

So the spelling is shared and the scope is disclosed on every render, in both
the populated and the empty case. A search that scopes to less than the caller
meant and returns nothing is this repo's house defect wearing a filter's
clothes: an absence produced by the tool, read as an absence in the world.

Three states, never two — `docs/validators.md` §"Declining instead of
guessing": rows, an empty result that says the search RAN, and a lookup that
could not run at all, which keeps its non-zero exit and prints no board.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parent.parent


def _load(name: str, rel: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


issues = _load("github_issues_1395", "presets/github/issues.py")
mrs = _load("gitlab_mrs_1395", "presets/gitlab/mrs.py")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _issue(number: int) -> dict:
    return {
        "number": number,
        "title": f"issue {number} about widgets",
        "state": "OPEN",
        "author": {"login": "someone"},
        "labels": [],
        "assignees": [],
        "milestone": None,
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
        "comments": [],
        "url": f"https://github.com/o/n/issues/{number}",
    }


def _mr(iid: int) -> dict:
    return {
        "iid": iid,
        "title": f"mr {iid} about widgets",
        "state": "opened",
        "source_branch": "f",
        "target_branch": "master",
        "updated_at": "2026-01-01T00:00:00Z",
        "web_url": f"https://gitlab.com/o/n/-/merge_requests/{iid}",
    }


def _run_issues(monkeypatch: pytest.MonkeyPatch, rows: list[dict],
                arg_str: str, rc: int = 0,
                stderr: str = "") -> tuple[int, str, str]:
    """Drive `gh-issues` with the one `gh issue list` call stubbed out."""
    payload = json.dumps(rows) if rc == 0 else ""

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, rc, payload, stderr)

    monkeypatch.setattr(issues.subprocess, "run", fake_run)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = issues.main_with_args(arg_str)
    return code, out.getvalue(), err.getvalue()


def _run_mrs(monkeypatch: pytest.MonkeyPatch, rows: list[dict],
             arg_str: str, rc: int = 0,
             stderr: str = "") -> tuple[int, str, str]:
    """Drive `gl-mrs` with the one `glab mr list` call stubbed out.

    Every call carries `nopipe`, so the list call is the only subprocess the
    op makes and nothing depends on the pipeline enrichment.
    """
    payload = json.dumps(rows) if rc == 0 else ""

    def fake_run(cmd: list[str], timeout: int = 25) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, rc, payload, stderr)

    monkeypatch.setattr(mrs, "_run", fake_run)
    monkeypatch.setattr(mrs, "_watched_iids", lambda: set())
    monkeypatch.setattr(mrs.sys, "argv", ["mrs.py", arg_str])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mrs.main()
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# the query reaches the forge, rather than filtering a widened page here
# ---------------------------------------------------------------------------

def test_gh_search_is_a_key_the_tokenizer_accepts() -> None:
    filters, _flags, unknown = issues._parse_args("search=widget")
    assert unknown == [], (
        f"`search=` must be an accepted filter key; got unknown={unknown!r}")
    assert filters == {"search": "widget"}, filters


def test_gh_search_is_pushed_to_github_not_applied_locally() -> None:
    cmd = issues._build_list_cmd({"search": "widget"}, 50)
    assert "--search" in cmd, (
        "the query must reach `gh issue list --search`; a client-side filter "
        f"over one page answers a different question. got {cmd!r}")
    assert cmd[cmd.index("--search") + 1] == "widget", cmd


def test_gl_search_is_a_key_the_tokenizer_accepts() -> None:
    filters, _flags, unknown = mrs._parse_args("search=widget")
    assert unknown == [], (
        f"`search=` must be an accepted filter key; got unknown={unknown!r}")
    assert filters == {"search": "widget"}, filters


def test_gl_search_is_pushed_to_gitlab_not_applied_locally() -> None:
    cmd = mrs._build_list_cmd({"search": "widget"}, 50)
    assert "--search" in cmd, (
        f"the query must reach `glab mr list --search`. got {cmd!r}")
    assert cmd[cmd.index("--search") + 1] == "widget", cmd


# ---------------------------------------------------------------------------
# which engine answered, and what it searched
# ---------------------------------------------------------------------------

def test_gh_populated_board_names_the_engine_and_the_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, err = _run_issues(monkeypatch, [_issue(1)], "search=widget,nopipe")
    assert code == 0, err
    assert "GitHub" in out, (
        f"the render must name the engine that answered. got {out!r}")
    assert "comments" in out and "body" in out, (
        "the render must state what was searched — GitHub's issue search "
        f"covers title, body and comments. got {out!r}")
    assert "widget" in out, out


def test_gl_populated_board_names_the_engine_and_the_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, err = _run_mrs(monkeypatch, [_mr(1)], "search=widget,nopipe")
    assert code == 0, err
    assert "GitLab" in out, (
        f"the render must name the engine that answered. got {out!r}")
    assert "description" in out, (
        "the render must state what was searched — GitLab searches title and "
        f"description. got {out!r}")


def test_the_two_engines_do_not_claim_the_same_scope() -> None:
    """One spelling over two engines is only honest if the scopes are stated.

    GitHub reads comments; GitLab does not. If these two sentences ever
    collapse into one, the shared `search=` key has become the lie this file
    argues it is not.
    """
    assert issues.SEARCH_SCOPE != mrs.SEARCH_SCOPE, (
        "gh-issues and gl-mrs must not advertise the same search scope: "
        f"{issues.SEARCH_SCOPE!r}")
    assert "comment" in issues.SEARCH_SCOPE.lower(), issues.SEARCH_SCOPE
    assert "comment" in mrs.SEARCH_SCOPE.lower(), (
        "gl-mrs must say comments are NOT searched, rather than staying "
        f"silent about the half GitHub covers and it does not: {mrs.SEARCH_SCOPE!r}")


# ---------------------------------------------------------------------------
# three states: rows, an empty result, and a search that could not run
# ---------------------------------------------------------------------------

def test_gh_empty_result_says_the_search_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, err = _run_issues(monkeypatch, [], "search=widget,nopipe")
    assert code == 0, err
    assert out.strip() != "No issues match.", (
        "a bare `No issues match.` under a search reads as a statement about "
        "the queue. It must name the query and the scope that produced the "
        f"zero. got {out!r}")
    assert "widget" in out, out
    assert "GitHub" in out, out


def test_gl_empty_result_says_the_search_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, err = _run_mrs(monkeypatch, [], "search=widget,nopipe")
    assert code == 0, err
    assert out.strip() != "No MRs match.", (
        f"the zero must name the query and the scope. got {out!r}")
    assert "widget" in out, out
    assert "GitLab" in out, out


@pytest.mark.parametrize("rows", [[], [1]])
def test_the_scope_sentence_is_printed_once_per_render(
    monkeypatch: pytest.MonkeyPatch, rows: list[int],
) -> None:
    """Above the board OR inside the empty sentence — never both.

    `gl-mrs` printed the disclosure unconditionally and then again inside its
    own `No MRs match ...` line, so the empty search render carried the same
    sentence twice, adjacent. A disclosure repeated verbatim is the one that
    gets skimmed, which is the failure mode every note in these boards exists
    to avoid. `gh-issues` guards the header print on `rows` and is the shape
    copied here.

    `gl-mrs`'s footer carries no search term, so one occurrence in the whole
    render is the invariant here. (`gh-issues` deliberately prints twice —
    header and footer — for the reason its cap note does: the consumer that
    truncates is the one that loses the footer.)
    """
    payload = [_mr(1)] if rows else []
    _code, out, _err = _run_mrs(monkeypatch, payload, "search=widget,nopipe")
    assert out.count("GitLab search over") == 1, (
        "the scope sentence must appear once above the board or once inside "
        f"the empty-result line, not both. got {out!r}")


def test_gh_a_search_that_could_not_run_is_never_an_empty_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, err = _run_issues(
        monkeypatch, [], "search=widget,nopipe", rc=1,
        stderr="Invalid search query")
    assert code == 1, "a failed search must not exit 0"
    assert "No issues match" not in out, (
        f"a lookup that never ran must not render as a zero-result board. "
        f"got {out!r}")
    assert "Invalid search query" in err, err


def test_gl_a_search_that_could_not_run_is_never_an_empty_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, err = _run_mrs(
        monkeypatch, [], "search=widget,nopipe", rc=1,
        stderr="400 Bad Request")
    assert code == 1, "a failed search must not exit 0"
    assert "No MRs match" not in out, out
    assert "400 Bad Request" in err, err


# ---------------------------------------------------------------------------
# composition
# ---------------------------------------------------------------------------

def test_gh_search_beside_iids_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`iids=` names an exact population; there is no listing to search.

    Forwarded to the lookup it would be dropped, and the caller would read N
    rows they enumerated as N rows that matched their query.
    """
    code, out, err = _run_issues(monkeypatch, [], "iids=1,2,search=widget,nopipe")
    assert code == 1, (
        f"a search beside an enumerated population must refuse. got {out!r}")
    assert "search" in err, err


def test_a_colon_in_the_query_refuses_and_fetches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mechanical reason GitHub's qualifier language is out of reach.

    `gh-issues:search=in:title widget` arrives as two argv segments, and the
    board reads one. This pins the refusal that makes the shared `search=`
    spelling honest — if it ever stopped firing, half a query would be sent
    and the board would answer a question nobody asked.
    """
    monkeypatch.setattr(
        issues.sys, "argv", ["issues.py", "search=in", "title widget"])
    called: list[object] = []

    def fake_run(*a: object, **k: object) -> subprocess.CompletedProcess[str]:
        called.append(a)
        return subprocess.CompletedProcess([], 0, "[]", "")

    monkeypatch.setattr(issues.subprocess, "run", fake_run)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = issues.main()
    assert code == 1, out.getvalue()
    assert called == [], "nothing may be fetched under a half-parsed query"
    assert "title widget" in err.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# the vocabulary is declared where a caller reads it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "preset,op",
    [("presets/github.json", "gh-issues"), ("presets/gitlab.json", "gl-mrs")],
)
def test_search_is_declared_in_the_op_syntax(preset: str, op: str) -> None:
    data = json.loads((ROOT / preset).read_text(encoding="utf-8"))
    entry = data["ops"][op]
    assert "search=" in entry["syntax"], (
        f"{op} accepts search= and its syntax does not say so; an undeclared "
        f"filter is one nobody finds. got {entry['syntax']!r}")
    assert "search" in entry["description"], entry["description"]
