"""`gh-prs` / `gl-mrs` must refuse a token they did not apply (#939).

#864 fixed this for `gh-issues` (shipped in #940): a token that is neither a
known flag nor a supported `key=value` is named and the call refuses. The two
sibling boards in the same preset family kept the original loop, whose last
branch is `elif tok in _FLAGS` with nothing after it — so an unrecognised token
falls off the end and the board is built as if nobody had asked for anything.

Reproduced live against real GitHub before a line was changed:

    gh-prs:milestone=nonexistent   -> all 5 open PRs, exit 0, no warning
    gh-prs:onlygreen               -> all 5 open PRs, exit 0, no warning

This is the house defect with the sign flipped — a *failure to narrow*, read as
a property of the world — and it is the worse direction, because an empty board
invites suspicion and a full one does not.

Three things are pinned here, and the second and third are on the same lines as
the first:

1. A token the op has never heard of (bare token, or `key=` whose key is not
   forwarded) refuses, names itself, and lists what would have been accepted.
2. A *known* key carrying a value the op cannot map is the same defect wearing
   a recognised name: `state=mergd` currently emits no `--state` flag at all, so
   the open board renders as the merged one.
3. `per=abc` silently reverts to the default page size, so a caller who asked
   for a different window is told nothing about not getting it.

The bar for every case: exit non-zero *and* no row on stdout. A refusal that
still prints the board is the bug wearing a warning label.

What is deliberately NOT refused: a value the backend rejects or matches
nothing (`label=nosuchlabel`). The op forwards that key, so the answer comes
from GitHub/GitLab and an empty board is the truth. Only tokens this op would
have dropped on the floor are its business.
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


prs = _load("github_prs_939", "presets/github/prs.py")
mrs = _load("gitlab_mrs_939", "presets/gitlab/mrs.py")
issues = _load("github_issues_939", "presets/github/issues.py")


# ---------------------------------------------------------------------------
# fixtures — one board row per platform, complete enough to render
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


def _run_prs(monkeypatch: pytest.MonkeyPatch, rows: list[dict],
             arg_str: str) -> tuple[int, str, str]:
    payload = json.dumps(rows)

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
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

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, payload, "")

    monkeypatch.setattr(mrs, "_run", fake_run)
    monkeypatch.setattr(mrs, "_watched_iids", lambda *a, **k: set())
    monkeypatch.setattr(sys, "argv", ["mrs.py", arg_str])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mrs.main()
    return code, out.getvalue(), err.getvalue()


def _no_board(out: str, marks: list[str]) -> None:
    for mark in marks:
        assert mark not in out, (
            f"a board must not be printed for a filter nobody applied; "
            f"found {mark!r} in:\n{out}"
        )


# ---------------------------------------------------------------------------
# (1) a token the op has never heard of
# ---------------------------------------------------------------------------

def test_prs_parse_args_hands_back_what_it_could_not_place() -> None:
    filters, flags, unknown = prs._parse_args("author=@me,onlygreen,nopipe")
    assert filters == {"author": "@me"}
    assert flags == {"nopipe"}
    assert unknown == ["onlygreen"], (
        f"a bare token that is not a known flag must be returned so the caller "
        f"can refuse; got {unknown!r}"
    )


def test_prs_parse_args_reports_a_filter_key_it_does_not_forward() -> None:
    """`milestone=` has no `gh pr list` flag, so `_build_list_cmd` drops it."""
    _filters, _flags, unknown = prs._parse_args("milestone=nonexistent")
    assert unknown == ["milestone=nonexistent"], (
        f"a key the op cannot forward must be reported, not accepted and then "
        f"dropped when the argv is built; got {unknown!r}"
    )


def test_mrs_parse_multi_reports_what_it_could_not_place() -> None:
    _multi, _flags, unknown = mrs._parse_multi("author=@me,onlygreen,milestne=v1")
    assert unknown == ["onlygreen", "milestne=v1"], (
        f"the multi-value tokenizer is the one radar shares, so it must report "
        f"too; got {unknown!r}"
    )


def test_prs_unknown_token_refuses_instead_of_printing_the_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, err = _run_prs(monkeypatch, [_pr(11), _pr(12)], "onlygreen,nopipe")
    assert code == 1, "an unrecognised token must not report success"
    _no_board(out, ["#11", "#12", "pr title 11"])
    assert "onlygreen" in err, err


def test_prs_unknown_filter_key_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, err = _run_prs(
        monkeypatch, [_pr(11), _pr(12)], "milestone=nonexistent,nopipe")
    assert code == 1, "a key gh-prs cannot forward must not report success"
    _no_board(out, ["#11", "#12", "pr title 11"])
    assert "milestone" in err, err


def test_mrs_unknown_token_refuses_instead_of_printing_the_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, err = _run_mrs(monkeypatch, [_mr(21), _mr(22)], "onlygreen,nopipe")
    assert code == 1, "an unrecognised token must not report success"
    _no_board(out, ["!21", "!22", "mr title 21"])
    assert "onlygreen" in err, err


def test_mrs_unknown_filter_key_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, err = _run_mrs(
        monkeypatch, [_mr(21), _mr(22)], "milestne=v18.9,nopipe")
    assert code == 1, "a typo'd key must not report success"
    _no_board(out, ["!21", "!22", "mr title 21"])
    assert "milestne" in err, err


def test_iids_output_is_suppressed_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """`iids` feeds watchers — an unfiltered id list is the worst payload."""
    code, out, err = _run_prs(monkeypatch, [_pr(11), _pr(12)], "onlygreen,iids")
    assert code == 1, err
    _no_board(out, ["11", "12"])


# ---------------------------------------------------------------------------
# the refusal has to be usable, not merely non-zero
# ---------------------------------------------------------------------------

def test_prs_refusal_names_every_bad_token_and_what_was_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, _out, err = _run_prs(monkeypatch, [_pr(11)], "onlygreen,milestone=x,nopipe")
    assert code == 1
    assert "onlygreen" in err and "milestone=x" in err, (
        f"every unapplied token must be named, not just the first: {err}"
    )
    for accepted in ("author", "reviewer", "label", "state"):
        assert accepted in err, f"the accepted filters must be listed: {err}"
    for accepted in ("nopipe", "iids", "failed"):
        assert accepted in err, f"the accepted flags must be listed: {err}"


def test_mrs_refusal_names_every_bad_token_and_what_was_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, _out, err = _run_mrs(monkeypatch, [_mr(21)], "onlygreen,milestne=x,nopipe")
    assert code == 1
    assert "onlygreen" in err and "milestne=x" in err, err
    for accepted in ("author", "reviewer", "label", "milestone", "state"):
        assert accepted in err, f"the accepted filters must be listed: {err}"


def test_the_three_boards_refuse_in_one_voice() -> None:
    """One helper, not three re-derivations — #628's anti-drift rule.

    Two independently written refusal paths in one preset family is how they
    disagree again in three months, which is the very asymmetry this issue was
    filed about.
    """
    messages = [
        issues._unknown_error(["zzz"]),
        prs._unknown_error(["zzz"]),
        mrs._unknown_error(["zzz"]),
    ]
    heads = {m.split(".")[0] for m in messages}
    assert len(heads) == 1, f"the refusal must read the same in all three: {heads}"


# ---------------------------------------------------------------------------
# (2) a known key whose value the op cannot map — same line, different bug
# ---------------------------------------------------------------------------

def test_prs_unmappable_state_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """`state=mergd` emits no `--state`, so the open board renders as merged."""
    code, out, err = _run_prs(monkeypatch, [_pr(11), _pr(12)], "state=mergd,nopipe")
    assert code == 1, (
        "a state the op cannot map must not silently fall back to gh's default"
    )
    _no_board(out, ["#11", "#12"])
    assert "mergd" in err and "merged" in err, (
        f"the refusal must name the bad value and the accepted ones: {err}"
    )


def test_mrs_unmappable_state_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, err = _run_mrs(monkeypatch, [_mr(21)], "state=mergd,nopipe")
    assert code == 1, err
    _no_board(out, ["!21"])
    assert "mergd" in err and "merged" in err, err


def test_issues_unmappable_state_refuses() -> None:
    """#864 did not close this half on `gh-issues` either."""
    _filters, _flags, unknown = issues._parse_args("state=opne")
    bad = issues._bad_values({"state": "opne"})
    assert unknown == [] and bad, (
        "state=opne is a known key with an unmappable value — it must be "
        f"caught as a bad value, got unknown={unknown!r} bad={bad!r}"
    )


# ---------------------------------------------------------------------------
# (3) per= — the third silent drop on the same code path
# ---------------------------------------------------------------------------

def test_prs_non_numeric_per_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, err = _run_prs(monkeypatch, [_pr(11)], "per=abc,nopipe")
    assert code == 1, "a page size the op cannot honour must be said, not dropped"
    _no_board(out, ["#11"])
    assert "per" in err and "abc" in err, err


def test_mrs_non_numeric_per_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, err = _run_mrs(monkeypatch, [_mr(21)], "per=abc,nopipe")
    assert code == 1, err
    _no_board(out, ["!21"])
    assert "per" in err and "abc" in err, err


# ---------------------------------------------------------------------------
# the refusal must not become a new false negative
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("arg_str", [
    "",
    "nopipe",
    "author=@me,failed,iids",
    "author=@me,state=merged,nopipe",
    "reviewer=@me,nopipe",
    "label=bug,nopipe",
    "assignee=@me,nopipe",
    "per=10,nopipe",
])
def test_prs_valid_arg_strings_still_work(
    monkeypatch: pytest.MonkeyPatch, arg_str: str,
) -> None:
    code, _out, err = _run_prs(monkeypatch, [_pr(11)], arg_str)
    assert code == 0, f"{arg_str!r} must still be accepted: {err}"


@pytest.mark.parametrize("arg_str", [
    "",
    "nopipe",
    "author=@me,state=opened,iids",
    "author=@me,failed,iids",
    "reviewer=@me,nopipe",
    "milestone=v18.9,nopipe",
    "source-branch=x,target-branch=master,nopipe",
    "author=@me,author=other,nopipe",
    "per=10,nopipe",
])
def test_mrs_valid_arg_strings_still_work(
    monkeypatch: pytest.MonkeyPatch, arg_str: str,
) -> None:
    code, _out, err = _run_mrs(monkeypatch, [_mr(21)], arg_str)
    assert code == 0, f"{arg_str!r} must still be accepted: {err}"


def test_every_in_repo_caller_arg_string_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one way this change could bite: a caller relying on a dropped token.

    `defaults.DEFAULT_FILTER` is what radar, the feed and watch-mine.sh all
    resolve to, so a refusal there would break every "watch everything of
    mine" flow at once.
    """
    defaults = _load("watch_defaults_939", "presets/watch/defaults.py")
    _multi, _flags, unknown = mrs._parse_multi(defaults.DEFAULT_FILTER)
    assert unknown == [], (
        f"DEFAULT_FILTER {defaults.DEFAULT_FILTER!r} would now be refused: "
        f"{unknown!r}"
    )
    code, _out, err = _run_mrs(
        monkeypatch, [_mr(21)], f"{defaults.DEFAULT_FILTER},iids")
    assert code == 0, err


def test_the_radar_gh_prs_tier_still_names_an_unhonourable_filter() -> None:
    """#954's tier re-derived this refusal by hand, over `set(filters) - KNOWN`.

    Once the op stops putting an unrecognised key into `filters` at all, that
    subtraction is empty and the tier's refusal goes silent — the fix to one
    op quietly disarming the guard in another. It parses through the shared
    tokenizer now, against its own (deliberately narrower) vocabulary.
    """
    tier = _load("radar_gh_prs_939", "presets/watch/tiers/gh_prs.py")
    with pytest.raises(tier.RadarError) as exc:
        tier.resolve_filter("milestone=v19")
    assert "milestone" in str(exc.value)

    with pytest.raises(tier.RadarError) as exc:
        tier.resolve_filter("failed")
    assert "failed" in str(exc.value), (
        "`failed` is a board shape gh-prs offers and this tier does not — a "
        "radar board silently narrowed to the failing rows is the defect"
    )

    # `nopipe` used to be accepted here and applied nowhere; #973 moved it into
    # the refused column beside `failed`, on both tiers at once.
    with pytest.raises(tier.RadarError) as exc:
        tier.resolve_filter("author=@me,nopipe")
    assert "nopipe" in str(exc.value)

    assert tier.resolve_filter("author=@me") == {"author": "@me"}


def test_the_tier_vocabulary_stays_narrower_than_the_op_it_shares_a_parser_with() -> None:
    """Sharing the tokenizer must not import the op's wider vocabulary.

    `iids` and `failed` are board *shapes* `gh-prs` offers: a bare list of
    numbers, and only the failing rows. Either one accepted here would narrow a
    radar board without saying so, which is the same lie as widening it. The op
    places both as flags; the tier must place neither.
    """
    tier = _load("radar_gh_prs_939_narrow", "presets/watch/tiers/gh_prs.py")

    for shape in ("iids", "failed"):
        assert shape in prs._FLAGS, (
            f"{shape!r} must be a flag the op accepts, or this test proves nothing"
        )
        assert prs._parse_args(shape)[2] == [], "the op accepts it"
        with pytest.raises(tier.RadarError, match=shape):
            tier.resolve_filter(shape)

    assert tier.KNOWN_FLAGS < prs._FLAGS, "the tier's flag set is a strict subset"
    assert tier.KNOWN_FILTERS < prs._FILTER_KEYS, "and so is its filter set"


def test_the_tier_refusal_names_only_tokens_that_were_actually_typed() -> None:
    """The old message joined two lists with no separator between them.

        named = ", ".join(f"{k}=" for k in bad) + ", ".join(unknown_flags)

    With one unknown key and one unknown flag those two joins abutted, so
    `milestone=x,onlygreen` was refused as `'milestone=onlygreen'` and
    `zeta,alpha=1` as `'alpha=zeta'` — a `key=value` pair the caller never
    wrote, naming a value that came from a different token. An error that
    misquotes the input sends the reader looking for a token that is not there.
    """
    tier = _load("radar_gh_prs_939_msg", "presets/watch/tiers/gh_prs.py")

    with pytest.raises(tier.RadarError) as exc:
        tier.resolve_filter("milestone=x,onlygreen")
    msg = str(exc.value)
    assert "milestone=, onlygreen" in msg
    assert "milestone=onlygreen" not in msg, (
        "the two unapplied tokens must not fuse into one invented token"
    )

    with pytest.raises(tier.RadarError) as exc:
        tier.resolve_filter("zeta,alpha=1")
    assert "zeta, alpha=" in str(exc.value)
    assert "alpha=zeta" not in str(exc.value)


def test_the_tier_accepts_and_refuses_exactly_what_it_did_before_939() -> None:
    """Sharing the tokenizer changed the wording; it must not change the verdict.

    The corpus is every shape the two implementations could disagree on: a
    stripped key, an empty key, a flag written as `key=`, a repeated key, and
    the two vocabularies' difference. The expected column is what the pre-#939
    tier returned, read off dc1ab4c.

    One row moved deliberately since, and it is called out rather than quietly
    edited: `nopipe` was accepted and never applied, so #973 refused it and
    dropped the flag half of the return value with it. Everything else must
    still answer exactly as it did.
    """
    tier = _load("radar_gh_prs_939_corpus", "presets/watch/tiers/gh_prs.py")

    accepted = {
        "": {},
        " author = me ": {"author": "me"},
        "author=a,author=b": {"author": "b"},
        "state=open,label=bug": {"state": "open", "label": "bug"},
    }
    for arg, expected in accepted.items():
        assert tier.resolve_filter(arg) == expected, arg

    for arg in ("milestone=v19", "per=5", "onlygreen", "iids", "failed",
                "nopipe", "state=open,label=bug,nopipe",
                "nopipe=1", "=x", "author=a,milestone=v19"):
        with pytest.raises(tier.RadarError):
            tier.resolve_filter(arg)


def test_the_mr_feed_poller_declines_a_scope_it_cannot_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one non-board caller of the shared tokenizer.

    A watcher scope with an unapplied token describes a *wider* population than
    the caller asked for, so building it anyway spawns pollers over strangers'
    MRs and fires an mr_opened for each. `None` is this function's established
    "could not establish the population" — the safe direction, since an empty
    dict would fire a departure for every MR instead.
    """
    feed = _load(
        "gitlab_mr_feed_939",
        "presets/watch/sources/gitlab-mr-feed/poller.py",
    )

    def _must_not_run(*_a: object, **_k: object) -> None:
        raise AssertionError("glab must not be called for an unapplied scope")

    monkeypatch.setattr(feed.mrs, "_run", _must_not_run)
    assert feed.fetch_population("author=@me,onlygreen") is None


def test_a_value_the_backend_rejects_is_not_this_ops_business(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`label=nosuchlabel` is forwarded — an empty board there is the truth."""
    code, _out, err = _run_prs(monkeypatch, [], "label=nosuchlabel,nopipe")
    assert code == 0, (
        f"a key the op forwards must reach the backend rather than being "
        f"pre-judged here: {err}"
    )
