"""`gh-issues:iids=N,N,N` — the bulk citation lookup (#1323).

The issue proposes a new op (`gh-titles`). It is the wrong shape: a bulk issue
lookup is the same *model* as the board — same population, same tracker-text
flattening, same three-state absence handling — differing only in how the
population is named. So it is a filter on `gh-issues`, and it inherits `per=`,
the disclosure machinery and the refusal.

The issue also asserts "`gh-prs` already takes `iids`, so the first spelling
costs a filter rather than a concept". That is false: `iids` is a *flag* on
both `gh-prs` and `gh-issues` (`_FLAGS`, render numbers only). There was no
value filter to copy anywhere in the family.

What every test below pins, and what fails on the code as it stands:

1. The tokenizer splits on `,`, so `iids=1233,1240,1251` arrived as
   `iids=1233` plus two orphans. The refusal that produced is correct and must
   survive for a genuinely unknown token — but a list value has to reach the op
   whole.
2. **A number that does not resolve gets its own row.** Never a shorter list.
   An audit over a short list reads as "all of these check out", which is the
   exact reading #1233's audit had to avoid: 12 of its 124 numbers belonged to
   another repo.
3. **A PR number is a third answer**, not the same answer as a number that does
   not exist. GraphQL's `issue(number:)` returns null for both.
4. `gh api graphql` **exits non-zero** when any alias is NOT_FOUND, while still
   returning every alias that resolved. Reading the exit code alone throws away
   the whole chunk — this repo's defect class, on the read side.
5. A requested population is finite and named, so `capped at --limit N — more
   may exist` is a false sentence there and must not print. A cap only exists
   if the caller asked for one with `per=`, and then it says how many it lost.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parent.parent
PRESET_PATH = ROOT / "presets" / "github" / "issues.py"
_spec = importlib.util.spec_from_file_location("github_issues_1323", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
issues = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(issues)

_ft_spec = importlib.util.spec_from_file_location(
    "filter_tokens_1323", ROOT / "presets" / "_filter_tokens.py")
assert _ft_spec is not None and _ft_spec.loader is not None
filter_tokens = importlib.util.module_from_spec(_ft_spec)
_ft_spec.loader.exec_module(filter_tokens)


class _Result:
    def __init__(self, code: int, out: str = "", err: str = "") -> None:
        self.returncode = code
        self.stdout = out
        self.stderr = err


def _issue_node(number: int, title: str, state: str = "OPEN", **kw: Any) -> dict:
    node: dict[str, Any] = {
        "number": number,
        "title": title,
        "state": state,
        "url": f"https://github.com/o/r/issues/{number}",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-02T00:00:00Z",
        "lastEditedAt": None,
        "authorAssociation": "OWNER",
        "author": {"login": "fdaviddpt"},
        "milestone": None,
        "labels": {"nodes": []},
        "assignees": {"nodes": []},
        "comments": {"totalCount": 0, "nodes": []},
        "closedByPullRequestsReferences": {"nodes": []},
        "timelineItems": {"nodes": []},
    }
    node.update(kw)
    return node


def _fake_gh(repository: dict, errors: list | None = None, code: int = 0):
    """Stand in for every subprocess this op makes on the iids path."""
    calls: list[list[str]] = []

    def run(cmd, **kwargs):  # noqa: ANN001
        calls.append(list(cmd))
        if cmd[:3] == ["gh", "repo", "view"]:
            return _Result(0, json.dumps({"name": "r", "owner": {"login": "o"}}))
        if cmd[:3] == ["gh", "api", "graphql"]:
            payload: dict[str, Any] = {"data": {"repository": repository}}
            if errors:
                payload["errors"] = errors
            return _Result(code, json.dumps(payload))
        raise AssertionError(f"unexpected subprocess: {cmd}")

    run.calls = calls  # type: ignore[attr-defined]
    return run


# ---------------------------------------------------------------------------
# 1. the tokenizer has to carry a list value whole
# ---------------------------------------------------------------------------

class TestListValuedKey:
    def test_comma_separated_value_survives_the_tokenizer(self) -> None:
        filters, flags, unknown = filter_tokens.parse(
            "iids=1233,1240,1251", {"iids"}, set(), list_keys={"iids"})
        assert unknown == []
        assert filters == {"iids": "1233,1240,1251"}
        assert flags == set()

    def test_a_bare_token_after_a_non_list_key_is_still_refused(self) -> None:
        _, _, unknown = filter_tokens.parse(
            "label=bug,1240", {"label", "iids"}, set(), list_keys={"iids"})
        assert unknown == ["1240"]

    def test_a_flag_ends_the_list_and_does_not_join_it(self) -> None:
        filters, flags, unknown = filter_tokens.parse(
            "iids=1,2,nopipe", {"iids"}, {"nopipe"}, list_keys={"iids"})
        assert filters == {"iids": "1,2"}
        assert flags == {"nopipe"}
        assert unknown == []

    def test_a_bare_token_after_the_flag_is_not_readopted(self) -> None:
        _, _, unknown = filter_tokens.parse(
            "iids=1,nopipe,2", {"iids"}, {"nopipe"}, list_keys={"iids"})
        assert unknown == ["2"]

    def test_default_has_no_list_keys_so_nothing_else_changes(self) -> None:
        _, _, unknown = filter_tokens.parse("author=me,1240", {"author"}, set())
        assert unknown == ["1240"]

    def test_a_non_numeric_member_is_a_value_error_not_a_silent_drop(self) -> None:
        bad = filter_tokens.bad_values(
            {"iids": "1233,twelve"}, {"iids": filter_tokens.POSITIVE_INT_LIST})
        assert [k for k, _v, _e in bad] == ["iids"]

    def test_a_well_formed_list_passes_the_value_check(self) -> None:
        assert filter_tokens.bad_values(
            {"iids": "1233,1240"}, {"iids": filter_tokens.POSITIVE_INT_LIST}) == []


class TestParseIids:
    def test_order_is_preserved_and_duplicates_are_counted(self) -> None:
        numbers, dupes = issues._parse_iids("1240,1233,1240,1251")
        assert numbers == [1240, 1233, 1251]
        assert dupes == 1


# ---------------------------------------------------------------------------
# 2/3. three answers, one row each
# ---------------------------------------------------------------------------

class TestEveryRequestedNumberGetsARow:
    def test_missing_and_pr_numbers_are_rows_not_absences(
            self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        repository = {
            "i1233": _issue_node(1233, "the citation audit"),
            "i1326": None,
            "p1326": {"number": 1326, "title": "one host check", "state": "OPEN"},
            "i999999": None,
            "p999999": None,
        }
        monkeypatch.setattr(issues.subprocess, "run", _fake_gh(repository))
        rc = issues.main_with_args("iids=1233,1326,999999")
        out = capsys.readouterr().out
        assert rc == 0
        # every requested number appears, none of them silently dropped
        for number in ("1233", "1326", "999999"):
            assert f"#{number}" in out, out
        assert "is a PR" in out
        assert "does not resolve to an issue" in out
        assert "3 issue(s)" in out

    def test_the_footer_separates_unresolved_from_failed_enrichment(
            self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        repository = {
            "i1233": _issue_node(1233, "the citation audit"),
            "i999999": None, "p999999": None,
        }
        monkeypatch.setattr(issues.subprocess, "run", _fake_gh(repository))
        rc = issues.main_with_args("iids=1233,999999")
        out = capsys.readouterr().out
        assert rc == 0
        assert "1 requested number(s) are not issues here" in out
        # the enrichment clause is a different claim and must not absorb them
        assert "enrichment" not in out

    def test_a_closed_issue_does_not_render_as_an_open_one(
            self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        repository = {"i1233": _issue_node(1233, "shipped", state="CLOSED")}
        monkeypatch.setattr(issues.subprocess, "run", _fake_gh(repository))
        issues.main_with_args("iids=1233")
        assert "[closed]" in capsys.readouterr().out

    def test_state_is_three_valued_like_every_other_list_field(self) -> None:
        assert "[state:?]" in issues._flags({"number": 1})
        assert "[closed]" in issues._flags({"number": 1, "state": "CLOSED"})
        assert "closed" not in issues._flags({"number": 1, "state": "OPEN"})


# ---------------------------------------------------------------------------
# 4. a NOT_FOUND alias makes gh exit 1 with the rest of the data intact
# ---------------------------------------------------------------------------

class TestNonZeroExitWithData:
    def test_the_resolved_aliases_survive_a_not_found_exit(
            self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        repository = {
            "i1233": _issue_node(1233, "the citation audit"),
            "i999999": None, "p999999": None,
        }
        errors = [{"type": "NOT_FOUND", "path": ["repository", "i999999"],
                   "message": "Could not resolve to an Issue with the number of 999999."}]
        monkeypatch.setattr(issues.subprocess, "run",
                            _fake_gh(repository, errors=errors, code=1))
        rc = issues.main_with_args("iids=1233,999999")
        out = capsys.readouterr().out
        assert rc == 0
        assert "the citation audit" in out
        assert "999999" in out

    def test_an_error_that_is_not_not_found_is_named_not_swallowed(
            self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        errors = [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]
        monkeypatch.setattr(issues.subprocess, "run",
                            _fake_gh({}, errors=errors, code=1))
        rc = issues.main_with_args("iids=1233")
        captured = capsys.readouterr()
        both = (captured.out + captured.err).lower()
        assert rc == 1
        assert "rate_limited" in both or "rate limit" in both


# ---------------------------------------------------------------------------
# 5. the cap says what it did, and never claims more may exist
# ---------------------------------------------------------------------------

class TestCapDisclosure:
    def test_a_named_population_never_prints_more_may_exist(
            self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        repository = {f"i{n}": _issue_node(n, f"issue {n}") for n in range(1, 4)}
        monkeypatch.setattr(issues.subprocess, "run", _fake_gh(repository))
        rc = issues.main_with_args("iids=1,2,3")
        out = capsys.readouterr().out
        assert rc == 0
        assert "3 issue(s)" in out
        assert "more may exist" not in out

    def test_an_explicit_per_caps_and_says_by_how_much(
            self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        repository = {f"i{n}": _issue_node(n, f"issue {n}") for n in (1, 2)}
        monkeypatch.setattr(issues.subprocess, "run", _fake_gh(repository))
        issues.main_with_args("iids=1,2,3,per=2")
        out = capsys.readouterr().out
        assert "iids capped at per=2" in out
        assert "1 of 3" in out

    def test_duplicates_are_collapsed_and_the_collapse_is_stated(
            self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        repository = {"i1233": _issue_node(1233, "the citation audit")}
        monkeypatch.setattr(issues.subprocess, "run", _fake_gh(repository))
        issues.main_with_args("iids=1233,1233")
        assert "1 duplicate number(s) collapsed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# composition
# ---------------------------------------------------------------------------

class TestComposition:
    @pytest.mark.parametrize("token", ["label=bug", "state=all", "author=@me",
                                       "assignee=@me", "milestone=v1.0"])
    def test_a_listing_filter_beside_iids_is_refused_not_ignored(
            self, token: str, capsys: pytest.CaptureFixture) -> None:
        rc = issues.main_with_args(f"iids=1233,{token}")
        err = capsys.readouterr().err
        assert rc == 1
        assert token.split("=")[0] in err
        assert "iids" in err
        # it must refuse the *composition*, not fall through to the
        # unrecognised-token refusal that master gives for `iids=1233` itself
        assert "unrecognised" not in err

    def test_the_iids_flag_still_renders_numbers_only(
            self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        repository = {
            "i1233": _issue_node(1233, "the citation audit"),
            "i999999": None, "p999999": None,
        }
        monkeypatch.setattr(issues.subprocess, "run", _fake_gh(repository))
        rc = issues.main_with_args("iids=1233,999999,iids")
        out = capsys.readouterr().out
        assert rc == 0
        assert "1233" in out
        # the number that did not resolve is not silently absent from a list
        # whose whole purpose is to be another tool's input
        assert "999999" in out
        assert [line for line in out.splitlines() if line.startswith("#")]

    def test_bare_iids_flag_is_untouched_by_the_new_filter_key(self) -> None:
        _filters, flags, unknown = issues._parse_args("iids")
        assert flags == {"iids"} and unknown == []
