"""gh-issues — the issue triage board (#769).

The board's whole product is the *order* and the *three states*, so that is
what this suite asserts. Two properties in particular have to fail against the
implementation a reasonable person writes first:

* **Rank.** A naive board sorts by number, or by `updatedAt`. The tests below
  build lists whose number order and rank order disagree, so "rows came back"
  cannot pass them.
* **Unknown is not zero, and not False.** Every enrichment-derived field is
  three-valued. A naive `authorAssociation` reader returns False for a missing
  key ("not external"), a naive linked-PR reader returns `[]` ("no PR"), and
  both of those are claims the op did not earn. Under a ranking board they do
  not merely misreport — they sort the row into a position it did not earn.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

PRESET_PATH = Path(__file__).parent.parent / "presets" / "github" / "issues.py"
_spec = importlib.util.spec_from_file_location("github_issues", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
issues = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(issues)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _issue(number: int, **kw: object) -> dict:
    """A `gh issue list --json` row, with the board's derived keys defaulted.

    Derived keys default to None — *unknown* — because that is the state a row
    is in before enrichment has said anything about it, and the point of the
    suite is that unknown survives every path that does not overwrite it.
    """
    row = {
        "number": number,
        "title": f"issue {number}",
        "state": "OPEN",
        "author": {"login": "someone"},
        "labels": [],
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
        "comments": [],
        "url": f"https://github.com/o/n/issues/{number}",
        "_external": None,
        "_stale": None,
        "_linked": None,
        "_comments": 0,
    }
    row.update(kw)
    return row


# ---------------------------------------------------------------------------
# _parse_args — the filter grammar is shared with gh-prs (#628)
# ---------------------------------------------------------------------------

def test_parse_args_empty_is_defaults() -> None:
    assert issues._parse_args("") == ({}, set())


def test_parse_args_filters_and_flags() -> None:
    filters, flags = issues._parse_args("author=@me,state=closed,nopipe")
    assert filters == {"author": "@me", "state": "closed"}
    assert flags == {"nopipe"}


def test_parse_args_ignores_unknown_bare_token() -> None:
    filters, flags = issues._parse_args("label=bug,bogus")
    assert flags == set()
    assert filters == {"label": "bug"}


def test_parse_args_knows_the_board_flags() -> None:
    _, flags = issues._parse_args("iids,external,stale")
    assert flags == {"iids", "external", "stale"}


# ---------------------------------------------------------------------------
# _build_list_cmd
# ---------------------------------------------------------------------------

def test_build_cmd_does_not_default_to_author_me() -> None:
    """A triage board defaults to the queue, not to what I filed.

    gh-prs defaults to `author=@me` because "my PRs" is the question it
    answers. The question here is "what is in the queue", and the rows that
    matter most are the ones somebody else filed — so a default author filter
    would hide exactly the class the ranking exists to surface.
    """
    cmd = issues._build_list_cmd({}, 50)
    assert "--author" not in cmd
    assert cmd[:4] == ["gh", "issue", "list", "--json"]
    assert cmd[cmd.index("--limit") + 1] == "50"


def test_build_cmd_passes_filters() -> None:
    cmd = issues._build_list_cmd(
        {"author": "octocat", "label": "bug", "state": "closed", "assignee": "@me"}, 10
    )
    assert cmd[cmd.index("--author") + 1] == "octocat"
    assert cmd[cmd.index("--label") + 1] == "bug"
    assert cmd[cmd.index("--state") + 1] == "closed"
    assert cmd[cmd.index("--assignee") + 1] == "@me"


def test_build_cmd_open_state_emits_no_flag() -> None:
    assert "--state" not in issues._build_list_cmd({"state": "open"}, 50)


def test_build_cmd_honours_repo_target(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_REPO", "octo/other")
    cmd = issues._build_list_cmd({}, 50)
    assert cmd[cmd.index("--repo") + 1] == "octo/other"


# ---------------------------------------------------------------------------
# _external — GitHub's authorAssociation, not a hand-kept login list
# ---------------------------------------------------------------------------

def test_external_false_for_repo_members() -> None:
    for assoc in ("OWNER", "MEMBER", "COLLABORATOR"):
        assert issues._external(assoc) is False, assoc


def test_external_true_for_everyone_else() -> None:
    for assoc in ("NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN"):
        assert issues._external(assoc) is True, assoc


def test_external_unknown_when_association_absent() -> None:
    """Missing is unknown, never "internal".

    This is the whole three-state contract at its smallest. A reader that
    returns False here is asserting the reporter is one of us — the single
    claim that, when wrong, sends an external report to the bottom of the
    queue and drops the data-not-instructions boundary with it.
    """
    assert issues._external(None) is None
    assert issues._external("") is None


# ---------------------------------------------------------------------------
# _is_stale — newest comment vs the last time the body was written
# ---------------------------------------------------------------------------

def test_stale_when_comment_is_newer_than_the_body() -> None:
    assert issues._is_stale(
        newest_comment="2026-02-02T00:00:00Z",
        created_at="2026-01-01T00:00:00Z",
        last_edited_at=None,
    ) is True


def test_not_stale_when_the_body_was_edited_after_the_last_comment() -> None:
    """The test a `createdAt`-only comparison cannot pass.

    `lastEditedAt` is null on an issue nobody edited, so the tempting shortcut
    is to compare against `createdAt` and be done. That reports every
    discussed-then-rewritten issue as stale, which is the flag firing on the
    rows that were *just* brought up to date — the fastest way to teach a
    reader to ignore it.
    """
    assert issues._is_stale(
        newest_comment="2026-02-02T00:00:00Z",
        created_at="2026-01-01T00:00:00Z",
        last_edited_at="2026-03-03T00:00:00Z",
    ) is False


def test_not_stale_without_comments_even_when_unenriched() -> None:
    """Zero comments answers the question without asking GitHub anything.

    A body with nothing said after it cannot have been overtaken, whatever
    `lastEditedAt` turns out to be. Declining here would be declining a
    question already answered, and on this repo it is the majority of rows.
    """
    assert issues._is_stale(
        newest_comment=None, created_at="2026-01-01T00:00:00Z", last_edited_at=None
    ) is False


def test_stale_unknown_when_body_write_time_is_unknown() -> None:
    assert issues._is_stale(
        newest_comment="2026-02-02T00:00:00Z", created_at=None, last_edited_at=None
    ) is None


# ---------------------------------------------------------------------------
# cells — every one of them can say "?"
# ---------------------------------------------------------------------------

def test_linked_cell_distinguishes_none_from_could_not_ask() -> None:
    assert issues._linked_cell(None) == "? unknown"
    assert issues._linked_cell([]) == "· no PR"
    assert "#761" in issues._linked_cell([{"number": 761, "state": "MERGED"}])


def test_ext_cell_three_states() -> None:
    assert issues._ext_cell(True) == "!"
    assert issues._ext_cell(False) == " "
    assert issues._ext_cell(None) == "?"


def test_comments_cell_unknown_is_not_zero() -> None:
    assert issues._comments_cell(None) == "?c"
    assert issues._comments_cell(0) == "0c"
    assert issues._comments_cell(12) == "12c"


def test_stale_flag_marks_unknown_separately() -> None:
    assert issues._flags(_issue(1, _stale=True)) == " [stale]"
    assert issues._flags(_issue(1, _stale=None)) == " [stale?]"
    assert issues._flags(_issue(1, _stale=False)) == ""


# ---------------------------------------------------------------------------
# _rank_key / ordering — the product
# ---------------------------------------------------------------------------

def _known(number: int, **kw: object) -> dict:
    base = {"_external": False, "_stale": False, "_linked": [{"number": 1, "state": "OPEN"}]}
    base.update(kw)
    return _issue(number, **base)


def test_rank_puts_unrankable_rows_first() -> None:
    """A row nobody could enrich outranks every row that was.

    Not because it is urgent — because its rank inputs are unknown, so any
    position assigned to it is invented. Sorting it to the bottom is the
    failure #769 names: it does not misreport, it gets worked last for a
    reason that is not true. Sorting it to the top makes the gap visible to
    the one person who can resolve it.
    """
    unknown = _issue(2)
    external = _known(1, _external=True, _linked=[])
    ordered = issues._sorted([external, unknown])
    assert [i["number"] for i in ordered] == [2, 1]


def test_rank_external_outranks_stale_and_unlinked() -> None:
    external = _known(11, _external=True)
    stale = _known(10, _stale=True, _linked=[])
    ordered = issues._sorted([stale, external])
    assert [i["number"] for i in ordered] == [11, 10]


def test_rank_stale_outranks_merely_unlinked() -> None:
    stale = _known(21, _stale=True)
    unlinked = _known(20, _linked=[])
    ordered = issues._sorted([unlinked, stale])
    assert [i["number"] for i in ordered] == [21, 20]


def test_rank_unlinked_outranks_worked_on() -> None:
    unlinked = _known(31, _linked=[])
    worked = _known(30)
    assert [i["number"] for i in issues._sorted([worked, unlinked])] == [31, 30]


def test_rank_oldest_first_within_a_tier() -> None:
    young = _known(40, createdAt="2026-05-05T00:00:00Z", _linked=[])
    old = _known(41, createdAt="2026-01-01T00:00:00Z", _linked=[])
    assert [i["number"] for i in issues._sorted([young, old])] == [41, 40]


def test_rank_is_not_number_order() -> None:
    """The end-to-end assertion a `sorted(by number)` board cannot pass."""
    rows = [
        _known(1),
        _known(2, _linked=[]),
        _known(3, _stale=True),
        _known(4, _external=True),
        _issue(5),
    ]
    assert [i["number"] for i in issues._sorted(rows)] == [5, 4, 3, 2, 1]


# ---------------------------------------------------------------------------
# enrichment plumbing
# ---------------------------------------------------------------------------

def test_graphql_query_asks_for_every_number() -> None:
    q = issues._graphql_query("octo", "repo", [7, 8])
    assert "i7: issue(number: 7)" in q
    assert "i8: issue(number: 8)" in q
    assert "lastEditedAt" in q and "authorAssociation" in q


def test_owner_repo_from_issue_urls_costs_no_extra_call(monkeypatch) -> None:
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    assert issues._owner_repo([_issue(1)]) == ("o", "n")


def test_owner_repo_prefers_the_repo_target(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_REPO", "octo/other")
    assert issues._owner_repo([_issue(1)]) == ("octo", "other")


def test_apply_enrichment_leaves_unfetched_rows_unknown() -> None:
    """A partial answer enriches what it covered and nothing else.

    The chunked GraphQL call can succeed for one batch and fail for the next.
    Broadcasting the successful shape over the rest is how a row that was
    never asked about acquires a confident False.
    """
    rows = [_issue(1), _issue(2)]
    issues._apply_enrichment(rows, {1: {
        "authorAssociation": "NONE",
        "lastEditedAt": None,
        # GraphQL always returns a field that was requested, so a real answer
        # for a covered row carries an empty list rather than omitting the key.
        # Omitting it would now mean "unknown", which is the point of #782 —
        # and would make this fixture assert the wrong thing about row 1.
        "closedByPullRequestsReferences": {"nodes": []},
        "timelineItems": {"nodes": []},
    }})
    assert rows[0]["_external"] is True
    assert rows[0]["_linked"] == []
    assert rows[1]["_external"] is None
    assert rows[1]["_linked"] is None


def test_apply_enrichment_reads_linked_prs_from_the_closers_not_the_timeline() -> None:
    """Rewritten by #782: the timeline was the wrong source.

    It used to assert that a `CrossReferencedEvent` naming a PR made the issue
    linked. Measured against the real API, that conflates two different facts —
    a `Closes #N` body line and a bare prose mention produce the same event —
    so a merged PR that only name-dropped an issue pushed it down the rank.
    The timeline entries below are now *mentions*, and only the closer counts.
    """
    rows = [_issue(1)]
    issues._apply_enrichment(rows, {1: {
        "authorAssociation": "MEMBER",
        "lastEditedAt": None,
        "closedByPullRequestsReferences": {
            "nodes": [{"number": 761, "state": "MERGED"}]},
        "timelineItems": {"nodes": [
            {"__typename": "CrossReferencedEvent",
             "source": {"__typename": "Issue"}},
            {"__typename": "CrossReferencedEvent",
             "source": {"__typename": "PullRequest", "number": 999, "state": "MERGED"}},
        ]},
    }})
    assert rows[0]["_linked"] == [{"number": 761, "state": "MERGED"}]
    assert rows[0]["_mentions"] == [{"number": 999, "state": "MERGED"}]


def test_annotate_counts_comments_and_finds_the_newest() -> None:
    rows = [_issue(1, comments=[
        {"createdAt": "2026-01-02T00:00:00Z"},
        {"createdAt": "2026-03-04T00:00:00Z"},
    ])]
    issues._annotate(rows)
    assert rows[0]["_comments"] == 2
    assert rows[0]["_newest_comment"] == "2026-03-04T00:00:00Z"


def test_annotate_comment_count_unknown_when_field_absent() -> None:
    rows = [{"number": 1, "createdAt": "2026-01-01T00:00:00Z"}]
    issues._annotate(rows)
    assert rows[0]["_comments"] is None
    assert rows[0]["_newest_comment"] is None


def test_annotate_settles_stale_false_with_zero_comments() -> None:
    rows = [_issue(1, comments=[])]
    issues._annotate(rows)
    assert rows[0]["_stale"] is False


# ---------------------------------------------------------------------------
# footer — names the reason enrichment is missing
# ---------------------------------------------------------------------------

def test_footer_reports_counts_when_everything_is_known() -> None:
    rows = [_known(1, _external=True), _known(2, _linked=[])]
    footer = issues._footer(rows, reason=None)
    assert "2 issue(s)" in footer
    assert "1 external" in footer


def test_footer_says_unknown_and_says_why() -> None:
    rows = [_issue(1), _issue(2)]
    footer = issues._footer(rows, reason="gh api graphql failed: 502")
    assert "unknown" in footer
    assert "502" in footer
    assert "oldest-first" in footer
    assert "0 external" not in footer


def test_footer_says_when_the_board_hit_the_limit() -> None:
    """`50 issue(s)` under `--limit 50` is a count of the tool's bound.

    On a ranked board the truncation is worse than a wrong total: the rows
    that fell off the end were chosen by gh's default ordering, not by the
    rank, so the queue can be silently missing the row that should have
    sorted first.
    """
    rows = [_known(n) for n in range(1, 4)]
    assert "capped" in issues._footer(rows, reason=None, per_page=3)
    assert "capped" not in issues._footer(rows, reason=None, per_page=50)


def test_footer_distinguishes_nopipe_from_failure() -> None:
    rows = [_issue(1)]
    assert "nopipe" in issues._footer(rows, reason=issues.REASON_NOPIPE)


# ---------------------------------------------------------------------------
# main() — the filters that cannot be answered decline
# ---------------------------------------------------------------------------

class _Result:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _stub_gh(monkeypatch, rows: list[dict], graphql_ok: bool = True) -> None:
    def _run(cmd, **kw):  # noqa: ANN001
        if cmd[:3] == ["gh", "issue", "list"]:
            return _Result(json.dumps(rows))
        if not graphql_ok:
            return _Result(returncode=1, stderr="HTTP 502")
        payload = {"data": {"repository": {
            f"i{r['number']}": {
                "number": r["number"],
                "authorAssociation": "MEMBER",
                "lastEditedAt": None,
                "timelineItems": {"nodes": []},
            } for r in rows
        }}}
        return _Result(json.dumps(payload))
    monkeypatch.setattr(issues.subprocess, "run", _run)


def test_main_external_filter_declines_when_it_cannot_be_answered(monkeypatch, capsys) -> None:
    """A filter over an unknown field refuses rather than returning "none".

    `gh-issues:external` with no enrichment could plausibly print `No issues
    match.` — and that sentence is a claim that there are no external reports,
    which is the one thing the caller must not be told wrongly.
    """
    _stub_gh(monkeypatch, [{"number": 1, "title": "t", "createdAt": "2026-01-01T00:00:00Z",
                            "comments": [{"createdAt": "2026-02-01T00:00:00Z"}],
                            "url": "https://github.com/o/n/issues/1"}], graphql_ok=False)
    rc = issues.main_with_args("external")
    out = capsys.readouterr()
    assert rc == 1
    assert "No issues match" not in out.out
    assert "external" in (out.err + out.out)


def test_main_iids_prints_bare_numbers(monkeypatch, capsys) -> None:
    _stub_gh(monkeypatch, [{"number": 5, "title": "t", "createdAt": "2026-01-01T00:00:00Z",
                            "comments": [], "url": "https://github.com/o/n/issues/5"}])
    rc = issues.main_with_args("iids")
    assert rc == 0
    assert capsys.readouterr().out.strip() == "5"


def test_main_renders_unknown_rows_when_graphql_fails(monkeypatch, capsys) -> None:
    _stub_gh(monkeypatch, [{"number": 9, "title": "a title", "createdAt": "2026-01-01T00:00:00Z",
                            "comments": [{"createdAt": "2026-02-01T00:00:00Z"}],
                            "url": "https://github.com/o/n/issues/9"}], graphql_ok=False)
    rc = issues.main_with_args("")
    out = capsys.readouterr().out
    assert rc == 0
    assert "? unknown" in out
    assert "[stale?]" in out
    assert "unknown" in out.splitlines()[-1]
