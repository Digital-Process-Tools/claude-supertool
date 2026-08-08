"""The board states an absence it never established — `gh-prs` (#1071), `gh-issues` (#1067).

Both ops render two states where there are three:

* rows found
* rows exist but something excluded them — a filter, a page boundary
* genuinely nothing

`gh-prs` applies `--author @me` when no role filter is given (`_build_list_cmd`),
so on `Digital-Process-Tools/claude-remember` — two open PRs, both from outside
contributors — it prints `No PRs match.` / `0 PR(s)`. There is no flag that
suppresses the default either: `author=` is refused by `_filter_tokens` for
having no value, so the board of everyone's open PRs is currently unreachable.

`gh-issues` grew the `--limit` disclosure in its footer at birth, and the
`iids` shape returns before the footer is built (`main_with_args`, the
`if "iids" in flags` early return). `iids` is the *piping* shape, so the one
consumer that feeds a truncated list into something else is the one told
nothing. The same hole exists in `gh-prs:iids`, which additionally has no
`--limit` disclosure anywhere at all.

The bar for every test here: it fails on the code as it stands, and it would
fail again if the fix reported `0` where it should report unknown.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

_PRESETS = Path(__file__).parent.parent / "presets" / "github"


def _load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _PRESETS / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


prs = _load("github_prs_1071", "prs.py")
issues = _load("github_issues_1067", "issues.py")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _pr(number: int, **kw: object) -> dict:
    row: dict[str, Any] = {
        "number": number,
        "title": f"pr {number}",
        "state": "OPEN",
        "author": {"login": "outsider"},
        "headRefName": f"feat/{number}",
        "headRefOid": "0" * 40,
        "baseRefName": "main",
        "labels": [],
        "assignees": [],
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
        "statusCheckRollup": [],
        "additions": 1,
        "deletions": 0,
        "changedFiles": 1,
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
        "url": f"https://github.com/o/n/pull/{number}",
    }
    row.update(kw)
    return row


def _issue(number: int, **kw: object) -> dict:
    row: dict[str, Any] = {
        "number": number,
        "title": f"issue {number}",
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
    row.update(kw)
    return row


def _drive(mod: Any, monkeypatch: pytest.MonkeyPatch, arg_str: str,
           responder: Callable[[list[str]], object]) -> tuple[int, str, str]:
    """Run `main_with_args`, answering each subprocess call from `responder`.

    `responder` receives the argv and returns either a payload string (rc 0) or
    an exception instance to raise — the spawn failure a probe has to survive
    without turning into a claim about the world.
    """

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        answer = responder(cmd)
        if isinstance(answer, BaseException):
            raise answer
        return subprocess.CompletedProcess(cmd, 0, str(answer), "")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod, "_watched_numbers", lambda *a, **k: set(), raising=False)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mod.main_with_args(arg_str)
    return code, out.getvalue(), err.getvalue()


def _by_author(mine: list[dict], everyone: list[dict]) -> Callable[[list[str]], object]:
    """Answer the board call and the no-author probe with different populations."""

    def responder(cmd: list[str]) -> object:
        return json.dumps(mine if "--author" in cmd else everyone)

    return responder


# ---------------------------------------------------------------------------
# #1071 — the default author filter, and the fact that it is unreachable
# ---------------------------------------------------------------------------

def test_the_default_author_filter_can_be_suppressed() -> None:
    """There must be a way to ask for the board of everyone's open PRs.

    `author=` is refused (no value), `label=` does not count as a role, so
    today the answer to "what is open on this repo" cannot be asked at all.
    """
    assert "anyauthor" in prs._FLAGS, (
        "gh-prs needs a flag that suppresses the implicit author=@me; "
        f"flags are {sorted(prs._FLAGS)!r}"
    )
    cmd = prs._build_list_cmd({}, 50, any_author=True)
    assert "--author" not in cmd, (
        f"anyauthor must not send --author; got {cmd!r}"
    )
    assert "--author" in prs._build_list_cmd({}, 50), (
        "the default is unchanged when the flag is absent"
    )


def test_anyauthor_and_an_explicit_author_are_refused_together(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """They ask for opposite boards; picking one silently is the whole defect."""
    code, out, err = _drive(prs, monkeypatch, "anyauthor,author=someone",
                            _by_author([], []))
    assert code == 1, f"expected a refusal, got rc={code} out={out!r}"
    assert "anyauthor" in err and "author" in err


def test_empty_board_names_the_filter_that_emptied_it(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`No PRs match. / 0 PR(s)` on a repo with two open PRs is false.

    This is #1071 exactly: `claude-remember`, PR #325 and #323, both external.
    """
    code, out, _err = _drive(prs, monkeypatch, "",
                             _by_author([], [_pr(325), _pr(323)]))
    assert code == 0
    assert "author=@me" in out, (
        "an empty board under an implicit filter must name the filter; "
        f"got {out!r}"
    )
    assert "2" in out.split("0 PR(s)")[-1], (
        f"it must say how many rows the filter excluded; got {out!r}"
    )
    assert "anyauthor" in out, (
        f"and the way to see them; got {out!r}"
    )


def test_empty_board_with_nothing_open_says_the_filter_excluded_none(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The third state. An empty repo must not read like a filtered one."""
    code, out, _err = _drive(prs, monkeypatch, "", _by_author([], []))
    assert code == 0
    assert "none" in out.lower(), f"got {out!r}"


def test_empty_board_whose_probe_failed_reports_unknown_not_zero(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A spawn failure is the platform difference that bites here.

    Windows raises `FileNotFoundError [WinError 2]` where POSIX may not fail at
    all, so the probe must have its own "the tool failed" arm. Reporting
    `excluded none` off a call that never ran is this repo's defect class
    reproduced inside the fix for it.
    """

    def responder(cmd: list[str]) -> object:
        if "--author" in cmd:
            return json.dumps([])
        return FileNotFoundError(2, "No such file or directory: 'gh'")

    code, out, _err = _drive(prs, monkeypatch, "", responder)
    assert code == 0
    assert "UNKNOWN" in out, (
        f"a probe that did not run must not be rendered as an answer; got {out!r}"
    )
    assert "excluded none" not in out.lower(), (
        f"and must not claim the filter hid nothing; got {out!r}"
    )


def test_a_populated_board_still_names_the_default_author_filter(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`3 PR(s)` under an implicit filter reads as the repo's population."""
    code, out, _err = _drive(prs, monkeypatch, "",
                             _by_author([_pr(1), _pr(2)], [_pr(1), _pr(2), _pr(9)]))
    assert code == 0
    assert "author=@me" in out, f"got {out!r}"


def test_an_explicit_author_filter_needs_no_probe(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller wrote the filter; only the *implicit* one misleads.

    And the probe must not fire — one extra `gh pr list` per board is a real
    cost on a radar tick.
    """
    calls: list[list[str]] = []

    def responder(cmd: list[str]) -> object:
        calls.append(cmd)
        return json.dumps([])

    code, out, _err = _drive(prs, monkeypatch, "author=someone", responder)
    assert code == 0
    assert len(calls) == 1, f"expected exactly the board call; got {calls!r}"
    assert "excluded" not in out.lower(), f"got {out!r}"


def test_gh_prs_discloses_its_page_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """`gh-issues` says `capped at --limit N`; `gh-prs` says nothing at all."""
    code, out, _err = _drive(prs, monkeypatch, "per=2,nopipe,anyauthor",
                             _by_author([], [_pr(1), _pr(2)]))
    assert code == 0
    assert "capped at --limit 2" in out, (
        f"a board that came back exactly --limit long must say so; got {out!r}"
    )


def test_the_failed_flag_says_how_many_rows_it_hid(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`failed` is client-side, so an empty board is a filtered one."""
    code, out, _err = _drive(prs, monkeypatch, "failed,nopipe,anyauthor",
                             _by_author([], [_pr(1), _pr(2), _pr(3)]))
    assert code == 0
    assert "failed" in out and "3" in out, (
        f"an empty `failed` board must name the filter and its count; got {out!r}"
    )


def test_gh_prs_iids_discloses_the_cap_without_polluting_the_list(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`iids` exists to be piped, and it is the shape with no footer.

    The note must be on *stdout*: `_run_custom_op` returns a successful op's
    stdout and drops its stderr (#654), so stderr is a channel the caller of
    `supertool 'gh-prs:iids'` never sees. A `#` prefix keeps the number list
    machine-readable.
    """
    code, out, _err = _drive(prs, monkeypatch, "per=2,iids,anyauthor",
                             _by_author([], [_pr(1), _pr(2)]))
    assert code == 0
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert [ln for ln in lines if not ln.startswith("#")] == ["1", "2"], (
        f"the number list must stay parseable; got {out!r}"
    )
    assert any("capped at --limit 2" in ln and ln.startswith("#")
               for ln in lines), (
        f"the cap must be disclosed, as a comment, on stdout; got {out!r}"
    )


# ---------------------------------------------------------------------------
# #1067 — the population cap, and the shape that never mentions it
# ---------------------------------------------------------------------------

def test_gh_issues_iids_discloses_the_page_cap(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The live half of #1067.

    The rendered board has said `capped at --limit N` since the op was born;
    `iids` returns before the footer is built, so the piping shape — the one
    whose output becomes another tool's input — is the one told nothing.
    """
    code, out, _err = _drive(
        issues, monkeypatch, "per=2,iids",
        lambda _cmd: json.dumps([_issue(10), _issue(11)]))
    assert code == 0
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert [ln for ln in lines if not ln.startswith("#")] == ["10", "11"], (
        f"the number list must stay parseable; got {out!r}"
    )
    assert any("capped at --limit 2" in ln and ln.startswith("#")
               for ln in lines), (
        f"a truncated iids list must say so; got {out!r}"
    )


def test_gh_issues_client_side_filter_says_how_many_it_hid(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`gh-issues:external` over an all-internal board prints `No issues match.`

    True about the filter, and read as a statement about the queue.
    """
    rows = [_issue(10, comments=[]), _issue(11, comments=[])]

    def responder(cmd: list[str]) -> object:
        if cmd[:3] == ["gh", "api", "graphql"]:
            return json.dumps({"data": {"repository": {
                f"i{r['number']}": {
                    "number": r["number"],
                    "lastEditedAt": None,
                    "authorAssociation": "OWNER",
                    "closedByPullRequestsReferences": {"nodes": []},
                    "timelineItems": {"nodes": []},
                } for r in rows}}})
        return json.dumps(rows)

    code, out, _err = _drive(issues, monkeypatch, "external", responder)
    assert code == 0
    assert "external" in out and "2" in out.split("0 issue(s)")[-1], (
        f"an emptied board must name the filter and the count it dropped; "
        f"got {out!r}"
    )

def test_gh_prs_iids_does_not_annotate_a_complete_list(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`iids` carries only the notes that state an absence.

    The scope note on a populated board — "your PRs, not the repo's" — is a
    label, not a claim that anything is missing. In a footer a human reads it
    is useful; in a stream a script parses it is noise, and the numbers above
    it are complete for the filter that was applied.
    """
    code, out, _err = _drive(prs, monkeypatch, "iids",
                             _by_author([_pr(1), _pr(2)], [_pr(1), _pr(2)]))
    assert code == 0
    assert out.split() == ["1", "2"], (
        f"a complete list must come back bare; got {out!r}"
    )
