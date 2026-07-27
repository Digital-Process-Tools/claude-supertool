"""Tests for the shared triage-board row layout (presets/_board.py).

The layout is shared on purpose: `gl-mrs`, `radar` and `gh-prs` are one board
to the reader. #421 fixed the truncated-title / missing-branch defect on the
GitLab side; #424 existed only because the same layout had been copied into
`gh-prs`. These tests pin the layout itself, and pin that both boards render
through it — so the defect cannot be reintroduced in one board alone.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

PRESETS = Path(__file__).parent.parent / "presets"


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, PRESETS / relpath)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


board = _load("board", "_board.py")
mrs = _load("gitlab_mrs", "gitlab/mrs.py")
prs = _load("github_prs", "github/prs.py")


# ---------------------------------------------------------------------------
# branch_pair
# ---------------------------------------------------------------------------

def test_branch_pair_renders_source_arrow_target() -> None:
    assert board.branch_pair("max/foo", "master") == "max/foo -> master"


def test_branch_pair_marks_a_missing_side() -> None:
    assert board.branch_pair("max/foo", None) == "max/foo -> ?"
    assert board.branch_pair("", "master") == "? -> master"


def test_branch_pair_is_empty_when_neither_side_is_known() -> None:
    assert board.branch_pair(None, "") == ""


# ---------------------------------------------------------------------------
# render_row
# ---------------------------------------------------------------------------

def _row(**over) -> str:
    args = dict(
        sigil="!", ident="33173", watched=False, status="✗ phpstan2",
        appr="✓", age="1h", changes="4Δ", branches="max/gen -> master",
        flags="", title="Make the Generator module loadable and coverable",
    )
    args.update(over)
    return board.render_row(**args)


def test_row_is_two_lines_status_then_the_full_title() -> None:
    title = "Make the Generator module loadable and coverable"
    assert len(title) > 42, "fixture must exceed the old truncation budget"
    head, title_line = _row().split("\n")
    assert head == "  ✗ phpstan2       ✓  1h    4Δ  !33173  max/gen -> master"
    assert title_line == "        " + title


def test_row_without_a_title_stays_on_one_line() -> None:
    row = _row(title="")
    assert "\n" not in row
    assert row.endswith("max/gen -> master")


def test_row_marks_a_watched_row_with_the_eye() -> None:
    assert _row(watched=True).startswith("👁 ")
    assert _row(watched=False).startswith("  ")


def test_row_suffix_lands_on_the_status_line_not_the_title() -> None:
    head, title_line = _row(suffix="  [healed]").split("\n")
    assert head.endswith("[healed]")
    assert "healed" not in title_line


def test_row_trailing_whitespace_is_stripped_from_the_status_line() -> None:
    head = _row(branches="", flags="", title="").rstrip("\n")
    assert head == head.rstrip()
    assert head.endswith("!33173")


# ---------------------------------------------------------------------------
# both boards render through the shared layout — they may not drift apart
# ---------------------------------------------------------------------------

def test_gl_and_gh_rows_are_byte_identical_apart_from_the_sigil() -> None:
    """One reader, one board shape. The only difference a GitHub row is
    allowed is '#' where GitLab writes '!'."""
    title = "give each board row its branch and its full title"
    mr = {"iid": 423, "title": title, "source_branch": "feat/421", "target_branch": "master",
          "updated_at": "", "_pipeline": "", "_changes": 12}
    pr = {"number": 423, "title": title, "headRefName": "feat/421", "baseRefName": "master",
          "updatedAt": "", "_checks": "", "_changes": 12}
    gl = mrs._row(mr, {"423"}, True)
    gh = prs._row(pr, {"423"})
    assert "\n" in gl, "a row is two lines: status, then the full title"
    assert title in gl and title in gh
    assert gh == gl.replace("!423", "#423")


def test_both_boards_reuse_the_shared_title_indent() -> None:
    assert mrs.TITLE_INDENT == board.TITLE_INDENT
    assert prs.TITLE_INDENT == board.TITLE_INDENT
