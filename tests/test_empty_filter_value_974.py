"""An empty filter value must not widen the board in silence (#974).

#939 taught the shared tokenizer to refuse a token the board could not apply.
`author=` walks straight through that refusal, because the *key* is known — so
`parse` files it under `filters`, `unknown` stays empty, every consumer's
`if unknown:` guard is satisfied, and `_build_list_cmd`'s opening
`if not val: continue` then drops the flag on the floor.

On `gh-prs` that is worse than a dropped filter. The default board is "mine",
and the default is suppressed by:

    has_role = any(k in filters for k in ("author", "assignee", "reviewer"))

`author=` puts `author` in `filters` with an empty value, so `has_role` is
True, `--author @me` is never emitted, and `--author` is never emitted either.
The board silently widens from *mine* to *everyone's* while the scope line
still claims a filter was applied. That is #939's defect reached through the
one input shape #939's guard cannot see.

The half that is not cosmetic is the mr-feed poller: it derives its population
from the same parse, and a widened population spawns a watcher per stranger's
MR and fires an `mr_opened` for each.

The judgment this pins: an empty value is **refused**, not normalised away.
No key here can express "empty" to the backend — `gh pr list --label ""` is not
GitHub's `no:label`, and there is no `--author ""` meaning "anyone" — so
normalising would have to pick between dropping the key (the widening this
issue is about) and inventing a client-side vocabulary the server does not
share. Refusal is also what #939 chose for every other unappliable token, and
the poller already handles a refusal safely: its `unknown` guard returns
`None`, which is "population not established" — no events either way, no crash
in a long-lived process.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tokens = _load("filter_tokens_974", "presets/_filter_tokens.py")
prs = _load("github_prs_974", "presets/github/prs.py")
mrs = _load("gitlab_mrs_974", "presets/gitlab/mrs.py")
issues = _load("github_issues_974", "presets/github/issues.py")
tier = _load("radar_gh_prs_974", "presets/watch/tiers/gh_prs.py")
feed = _load("mr_feed_poller_974",
             "presets/watch/sources/gitlab-mr-feed/poller.py")


# ---------------------------------------------------------------------------
# harnesses — mirror tests/test_prs_mrs_unknown_token_939.py
# ---------------------------------------------------------------------------

def _pr(number: int) -> dict:
    return {
        "number": number,
        "title": f"pr title {number}",
        "state": "OPEN",
        "author": {"login": "someone"},
        "headRefName": f"feat/{number}",
        "baseRefName": "master",
        "labels": [],
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
        "statusCheckRollup": [],
        "additions": 1,
        "deletions": 1,
        "changedFiles": 1,
        "updatedAt": "2026-01-01T00:00:00Z",
        "createdAt": "2026-01-01T00:00:00Z",
        "assignees": [],
        "url": f"https://github.com/o/n/pull/{number}",
    }


def _mr(iid: int) -> dict:
    return {
        "iid": iid,
        "title": f"mr title {iid}",
        "state": "opened",
        "author": {"username": "someone"},
        "source_branch": f"feat/{iid}",
        "target_branch": "master",
        "labels": [],
        "draft": False,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "web_url": f"https://gitlab.com/o/n/-/merge_requests/{iid}",
    }


def _issue(number: int) -> dict:
    return {
        "number": number,
        "title": f"issue title {number}",
        "state": "OPEN",
        "author": {"login": "someone"},
        "labels": [],
        "assignees": [],
        "milestone": None,
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
        "url": f"https://github.com/o/n/issues/{number}",
        "comments": [],
        "body": "",
    }


def _run_prs(monkeypatch: pytest.MonkeyPatch, rows: list[dict],
             arg_str: str) -> tuple[int, str, str]:
    payload = json.dumps(rows)

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 0, payload, "")

    monkeypatch.setattr(prs.subprocess, "run", fake_run)
    monkeypatch.setattr(prs, "_watched_numbers", lambda *a, **k: set())
    monkeypatch.setattr(sys, "argv", ["prs.py", arg_str])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = prs.main()
    return code, out.getvalue(), err.getvalue()


def _run_mrs(monkeypatch: pytest.MonkeyPatch, rows: list[dict],
             arg_str: str) -> tuple[int, str, str]:
    payload = json.dumps(rows)

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 0, payload, "")

    monkeypatch.setattr(mrs, "_run", fake_run)
    monkeypatch.setattr(mrs, "_watched_iids", lambda *a, **k: set())
    monkeypatch.setattr(sys, "argv", ["mrs.py", arg_str])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mrs.main()
    return code, out.getvalue(), err.getvalue()


def _run_issues(monkeypatch: pytest.MonkeyPatch, rows: list[dict],
                arg_str: str) -> tuple[int, str, str]:
    payload = json.dumps(rows)
    graphql = json.dumps({"data": {"repository": {}}})

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess:
        body = graphql if "graphql" in cmd else payload
        return subprocess.CompletedProcess(cmd, 0, body, "")

    monkeypatch.setattr(issues.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["issues.py", arg_str])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = issues.main()
    return code, out.getvalue(), err.getvalue()


def _no_board(out: str, marks: list[str]) -> None:
    for mark in marks:
        assert mark not in out, (
            f"a refusal must not still print the board; found {mark!r} in:\n{out}"
        )


# ---------------------------------------------------------------------------
# (1) the tokenizer — where the hole is
# ---------------------------------------------------------------------------

_KEYS = {"author", "assignee", "reviewer", "label", "state"}
_FLAGS = {"nopipe"}


@pytest.mark.parametrize("tok", ["author=", "assignee=", "reviewer=",
                                 "label=", "state=", "author=   "])
def test_a_known_key_with_no_value_is_handed_back_not_filed(tok: str) -> None:
    """`author=` is not a filter — nothing downstream can apply it."""
    filters, _flags, unknown = tokens.parse_multi(tok, _KEYS, _FLAGS)
    key = tok.partition("=")[0].strip()
    # `parse_multi` strips each token before dispatching, so the token handed
    # back is the normalised spelling — `author=   ` is reported as `author=`.
    assert unknown == [tok.strip()], (
        f"{tok!r} names a key the board knows and a value it cannot use; it "
        f"must be returned so the caller can refuse, not filed under filters "
        f"where every `if unknown:` guard reads clean. got unknown={unknown!r}"
    )
    assert key not in filters, (
        f"{key!r} must not reach `filters` — `_build_list_cmd` drops it there "
        f"with `if not val: continue`, and on a role key that also suppresses "
        f"the `--author @me` default. got filters={filters!r}"
    )


def test_a_value_the_backend_may_reject_is_still_forwarded() -> None:
    """The line stays where #939 drew it — only *unappliable* tokens refuse."""
    filters, _flags, unknown = tokens.parse_multi(
        "label=nosuchlabel,author=@me", _KEYS, _FLAGS)
    assert unknown == []
    assert filters == {"label": ["nosuchlabel"], "author": ["@me"]}


def test_the_refusal_names_the_key_and_says_what_to_do() -> None:
    msg = tokens.unknown_error(["author="], _KEYS, _FLAGS)
    assert "author=" in msg
    assert "no value" in msg.lower(), (
        f"an empty value is not an unrecognised token — the key IS known. "
        f"Saying 'unrecognised' sends the reader hunting for a typo in a "
        f"spelling that is correct. got:\n{msg}"
    )


# ---------------------------------------------------------------------------
# (2) the boards — exit non-zero AND print no rows
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("arg", ["author=,nopipe", "assignee=,nopipe",
                                 "reviewer=,nopipe", "label=,nopipe"])
def test_gh_prs_refuses_an_empty_value_instead_of_widening(
        monkeypatch: pytest.MonkeyPatch, arg: str) -> None:
    code, out, err = _run_prs(monkeypatch, [_pr(11), _pr(12)], arg)
    assert code != 0, (
        f"gh-prs:{arg} must exit non-zero — it currently exits 0 having "
        f"answered a narrowing question with a WIDER board than the default "
        f"(`--author @me` is suppressed by the empty role key). stdout:\n{out}"
    )
    _no_board(out, ["pr title 11", "pr title 12", "#11", "#12"])
    assert arg.partition(",")[0] in err


def test_gh_prs_never_builds_the_query_for_an_empty_role_key(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The concrete widening, pinned at the argv."""
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "[]", "")

    monkeypatch.setattr(prs.subprocess, "run", fake_run)
    monkeypatch.setattr(prs, "_watched_numbers", lambda *a, **k: set())
    monkeypatch.setattr(sys, "argv", ["prs.py", "author=,nopipe"])
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        prs.main()
    listed = [c for c in seen if c[:3] == ["gh", "pr", "list"]]
    assert not listed, (
        f"no `gh pr list` may run for `author=`: the argv it would have built "
        f"carries neither `--author` nor the `--author @me` default, i.e. the "
        f"whole board. got {listed!r}"
    )


@pytest.mark.parametrize("arg", ["author=,nopipe", "label=,nopipe"])
def test_gl_mrs_refuses_an_empty_value(
        monkeypatch: pytest.MonkeyPatch, arg: str) -> None:
    code, out, err = _run_mrs(monkeypatch, [_mr(21)], arg)
    assert code != 0, f"gl-mrs:{arg} must refuse; stdout:\n{out}"
    _no_board(out, ["mr title 21", "!21"])
    assert arg.partition(",")[0] in err


@pytest.mark.parametrize("arg", ["author=", "milestone="])
def test_gh_issues_refuses_an_empty_value(
        monkeypatch: pytest.MonkeyPatch, arg: str) -> None:
    code, out, err = _run_issues(monkeypatch, [_issue(31)], arg)
    assert code != 0, f"gh-issues:{arg} must refuse; stdout:\n{out}"
    _no_board(out, ["issue title 31"])


def test_a_real_filter_still_reaches_the_query(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal must not be a new false negative (#939's own bar)."""
    code, out, _err = _run_prs(monkeypatch, [_pr(11)], "author=@me,nopipe")
    assert code == 0, f"a valid filter must still work; stdout:\n{out}"
    assert "pr title 11" in out


# ---------------------------------------------------------------------------
# (3) the derived consumers — the half that is not cosmetic
# ---------------------------------------------------------------------------

def test_mr_feed_poller_declines_a_scope_it_cannot_narrow(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`None` is "population not established" — no events, and no crash.

    The alternative the issue warns about is a widened population: one watcher
    per stranger's MR and an `mr_opened` for each. `None` is the guard the
    poller already owns for exactly this; it just could not see `author=`.
    """
    def explode(*_a: object, **_k: object) -> None:  # pragma: no cover
        raise AssertionError(
            "the poller must not query GitLab for a scope it could not "
            "narrow — that is the widened population the guard exists to stop")

    monkeypatch.setattr(feed.mrs, "_run", explode)
    pop, error = feed.fetch_population("author=")
    assert pop is None
    # #1602: the refusal was right and silent. It now says which token.
    assert "author=" in error


def test_mr_feed_poller_still_resolves_a_good_scope(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 0, json.dumps([_mr(21)]), "")

    monkeypatch.setattr(feed.mrs, "_run", fake_run)
    got, error = feed.fetch_population("author=@me")
    assert got is not None and "21" in got
    assert error == ""


def test_radar_gh_prs_tier_refuses_an_empty_value() -> None:
    with pytest.raises(tier.RadarError) as exc:
        tier.resolve_filter("author=")
    assert "author=" in str(exc.value)
