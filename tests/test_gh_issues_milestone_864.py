"""`gh-issues` and the milestone (#864).

The issue as filed says `milestone=` "is not among the accepted filters". It
has been since the op was born (`e67947a`) — `_build_list_cmd` forwards it to
`gh issue list --milestone` and `_LIST_FIELDS` already requests the field. What
is actually missing is three things, and the third is the dangerous one:

1. **The field is fetched and never rendered.** A board answering "what is in
   v0.26.0" prints no milestone on any row.
2. **There is no way to ask for the gap.** `gh issue list` can name a
   milestone; it cannot ask for issues that have none, which is the half of
   release planning that finds the unfiled work.
3. **An unrecognised token is silently dropped.** `_parse_args` keeps a token
   only if it parses as `key=value` or is in `_FLAGS`; everything else falls
   off the end of the loop. So `gh-issues:nomilestone` today returns the
   *entire unfiltered board* and the caller reads it as the answer to "which
   issues have no milestone". Same for a typo'd filter key — `milestne=v0.26.0`
   is discarded by `_build_list_cmd` and the whole queue renders as the
   contents of that milestone.

(3) is this repo's defect class with the sign flipped: not an absence produced
by the tool read as an absence in the world, but a *lack of filtering* produced
by the tool read as a property of the world. It is the same failure the issue
describes hitting with hand-written jq — a query whose entire job is finding
gaps returning something that is not the gaps — and it is louder here, because
a full board looks like a rich, healthy answer.

The bar: every test below fails on the code as it stands. A test that merely
asserted "the board renders" would pass on the broken op.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

PRESET_PATH = Path(__file__).parent.parent / "presets" / "github" / "issues.py"
_spec = importlib.util.spec_from_file_location("github_issues_864", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
issues = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(issues)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _issue(number: int, milestone: object = "__absent__", **kw: object) -> dict:
    """A `gh issue list --json` row.

    `milestone` defaults to the sentinel so a test can ask for all three
    states: a title, `None` (gh's "no milestone"), and the key being absent
    altogether (gh did not answer).
    """
    row: dict[str, Any] = {
        "number": number,
        "title": f"issue {number}",
        "state": "OPEN",
        "author": {"login": "someone"},
        "labels": [],
        "assignees": [],
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
        "comments": [],
        "url": f"https://github.com/o/n/issues/{number}",
    }
    if milestone != "__absent__":
        row["milestone"] = milestone
    row.update(kw)
    return row


def _board_row(number: int, **kw: object) -> dict:
    """A row as `_row()` receives it — post `_annotate`, post enrichment."""
    row = _issue(number, **kw)
    row.setdefault("_external", False)
    row.setdefault("_stale", False)
    row.setdefault("_linked", [])
    row.setdefault("_mentions", [])
    row.setdefault("_comments", 0)
    return row


def _run_op(monkeypatch: pytest.MonkeyPatch, rows: list[dict],
            arg_str: str) -> tuple[int, str, str]:
    """Drive `main_with_args` with `gh issue list` stubbed out.

    Every call in these tests carries `nopipe`, so the single subprocess call
    is the list call and nothing here depends on GraphQL enrichment —
    milestone is a list field.
    """
    payload = json.dumps(rows)

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, payload, "")

    monkeypatch.setattr(issues.subprocess, "run", fake_run)
    import io
    import contextlib
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = issues.main_with_args(arg_str)
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# (3) the unrecognised token — an unfiltered board rendering as a filtered one
# ---------------------------------------------------------------------------

def test_parse_args_reports_tokens_it_did_not_understand(
) -> None:
    """`_parse_args` must hand back what it could not place, not eat it."""
    filters, flags, unknown = issues._parse_args("label=bug,nosuchflag,stale")
    assert filters == {"label": "bug"}
    assert flags == {"stale"}
    assert unknown == ["nosuchflag"], (
        "a token that is neither key=value nor a known flag must be returned "
        "so the caller can refuse; silently dropping it renders an unfiltered "
        f"board as a filtered one. got {unknown!r}"
    )


def test_parse_args_reports_an_unknown_filter_key(
) -> None:
    """A typo'd key is dropped by `_build_list_cmd`, so it must be caught here.

    `milestne=v0.26.0` currently produces the whole queue under the reader's
    belief that it is one milestone's contents.
    """
    filters, _flags, unknown = issues._parse_args("milestne=v0.26.0")
    assert unknown == ["milestne=v0.26.0"], (
        f"an unsupported filter key must be reported, not forwarded and "
        f"ignored; got filters={filters!r} unknown={unknown!r}"
    )


def test_an_unknown_token_refuses_instead_of_printing_the_whole_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The behavioural half: exit non-zero, print no rows, name the token."""
    rows = [_issue(1, None), _issue(2, {"title": "v0.26.0"})]
    code, out, err = _run_op(monkeypatch, rows, "nomilestne,nopipe")
    assert code == 1, "an unrecognised token must not report success"
    assert "#1" not in out and "#2" not in out, (
        f"an unfiltered board must not be printed for a filter nobody "
        f"applied; got:\n{out}"
    )
    assert "nomilestne" in err, err


# ---------------------------------------------------------------------------
# (2) asking for the gap
# ---------------------------------------------------------------------------

def test_nomilestone_returns_only_the_issues_carrying_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [_issue(1, None), _issue(2, {"title": "v0.26.0"}), _issue(3, None)]
    code, out, err = _run_op(monkeypatch, rows, "nomilestone,nopipe")
    assert code == 0, err
    assert "#1" in out and "#3" in out
    assert "#2" not in out, f"an issue with a milestone must not survive:\n{out}"


def test_nomilestone_declines_when_a_rows_milestone_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three-state contract, at the point it costs something.

    A row whose `milestone` key never came back is not a row with no
    milestone. Filtering it in produces a gap report containing an issue that
    may well be scheduled; filtering it out produces one that silently omits
    an issue that may well not be. Both are wrong, so the op declines and says
    which field it could not establish.
    """
    rows = [_issue(1, None), _issue(2)]  # #2: key absent entirely
    code, out, err = _run_op(monkeypatch, rows, "nomilestone,nopipe")
    assert code == 1, f"expected a decline, got exit {code}:\n{out}"
    assert "#1" not in out, f"a partial gap report must not be printed:\n{out}"
    assert "milestone" in err.lower(), err


def test_bare_milestone_filter_still_reaches_gh(
) -> None:
    """The half the issue believed was missing — pinned so it cannot regress."""
    cmd = issues._build_list_cmd({"milestone": "v0.26.0"}, 50)
    assert "--milestone" in cmd and "v0.26.0" in cmd, cmd


# ---------------------------------------------------------------------------
# (1) rendering — three states, and no column tax on rows without one
# ---------------------------------------------------------------------------

def test_a_row_with_a_milestone_names_it() -> None:
    head = issues._row(_board_row(7, milestone={"title": "v0.26.0"})).splitlines()[0]
    assert "v0.26.0" in head, head


def test_a_row_without_a_milestone_pays_nothing_for_the_field() -> None:
    """No blank column. Most issues on most repos carry no milestone, and a
    mostly-blank column is width taken from every row to say nothing."""
    with_ms = issues._row(_board_row(7, milestone={"title": "v0.26.0"})).splitlines()[0]
    without = issues._row(_board_row(7, milestone=None)).splitlines()[0]
    assert "v0.26.0" in with_ms
    assert "v0.26.0" not in without
    assert len(without) < len(with_ms), (
        "a row with no milestone must be shorter than one with — a reserved "
        f"column would make them equal.\n{without!r}\n{with_ms!r}"
    )


def test_an_unknown_milestone_is_not_rendered_as_no_milestone() -> None:
    """`?`, not silence. Absent key = the tool did not get an answer."""
    unknown = issues._row(_board_row(7)).splitlines()[0]
    none = issues._row(_board_row(7, milestone=None)).splitlines()[0]
    assert unknown != none, (
        "'gh did not return the field' and 'this issue has no milestone' are "
        f"different answers and must not render identically: {unknown!r}"
    )
    assert "?" in unknown.split("#")[-1], unknown


# ---------------------------------------------------------------------------
# the cap disclosure, which a client-side filter silently voids
# ---------------------------------------------------------------------------

def test_the_limit_disclosure_survives_a_client_side_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--limit` bounds the *fetch*, so the cap is a fact about the fetch.

    `_footer` measures the cap against the rows it is handed. Filter three of
    four rows away client-side and the surviving count drops under the limit,
    so the "more may exist" line disappears — from the one query whose entire
    purpose is completeness. The gap report then reads as exhaustive while
    being the first `--limit` rows of an unknown number.
    """
    rows = [_issue(1, None), _issue(2, {"title": "v0.26.0"}),
            _issue(3, {"title": "v0.26.0"}), _issue(4, {"title": "v0.26.0"})]
    code, out, err = _run_op(monkeypatch, rows, "nomilestone,nopipe,per=4")
    assert code == 0, err
    assert "capped" in out, (
        "the fetch returned exactly --limit rows, so the board is a prefix of "
        f"an unknown number and must say so even after filtering:\n{out}"
    )
