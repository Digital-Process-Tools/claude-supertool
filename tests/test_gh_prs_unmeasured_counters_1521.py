"""Two counters rendered a measurement that never happened as one that found
nothing (#1521).

Both are this repository's house defect — an absence produced by the tool read as
an absence in the world (`docs/validators.md` §"Declining instead of guessing") —
and they sit on the two halves of the same op.

* `gh-prs:merged-since=DATE` threw `filter_merged`'s third return value away.
  The tag branch renders it as `UNPLACED: N`; the date branch discarded it, so a
  row whose `mergedAt` will not parse was neither listed nor disclosed and the op
  exited 0. The information existed at the call site and was dropped there.

* `assess` printed `boundary PR: none — no returned row's merge commit is the
  tagged commit` even when `split_tagged_commit` returned early on a falsy
  `boundary_sha` and compared nothing at all. Did-not-compare rendered as
  compared-and-found-none. It fails closed on the count, which is why it was
  filed as not blocking — but the sentence is a claim the code did not earn.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PRESETS = Path(__file__).parent.parent / "presets"
sys.path.insert(0, str(PRESETS))
sys.path.insert(0, str(PRESETS / "github"))


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PRESETS / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rg = _load("github/_release_gate.py", "release_gate_1521")
prs = _load("github/prs.py", "gh_prs_1521")

PLACED = {"number": 11, "title": "placed", "mergedAt": "2026-08-12T10:00:00Z"}
UNPLACED = {"number": 12, "title": "unplaced", "mergedAt": "not a date at all"}


# ---------------------------------------------------------------------------
# 1 — the date branch discarded the third state
# ---------------------------------------------------------------------------

def test_filter_merged_still_hands_the_date_branch_the_undated_rows() -> None:
    """The information exists. The bug was entirely at the call site."""
    kept, undated = rg.filter_merged(
        [PLACED, UNPLACED], rg.parse_instant("2026-08-01T00:00:00Z"))
    assert [r["number"] for r in kept] == [11]
    assert [r["number"] for r in undated] == [12]


def test_a_date_boundary_discloses_the_rows_it_could_not_place(capsys) -> None:
    code = prs._boundary_slice(
        rows=[PLACED, UNPLACED],
        plan=prs._GatePlan(value="2026-08-01", is_tag=False),
        boundary=None, filters={}, flags=set(), per_page=100)
    out = capsys.readouterr().out
    assert code == 0
    assert "UNPLACED: 1" in out, out
    assert "#12" in out, out


def test_a_date_boundary_that_placed_every_row_says_nothing_extra(capsys) -> None:
    """Hedging a number that was not hedged is the other half of the defect."""
    prs._boundary_slice(
        rows=[PLACED],
        plan=prs._GatePlan(value="2026-08-01", is_tag=False),
        boundary=None, filters={}, flags=set(), per_page=100)
    assert "UNPLACED" not in capsys.readouterr().out


def test_the_disclosure_survives_the_iids_render(capsys) -> None:
    """`iids` drops the rows to bare numbers; a truncated list and a complete
    one are the same bytes downstream, so the notes ride along as comments."""
    prs._boundary_slice(
        rows=[PLACED, UNPLACED],
        plan=prs._GatePlan(value="2026-08-01", is_tag=False),
        boundary=None, filters={}, flags={"iids"}, per_page=100)
    out = capsys.readouterr().out
    assert "# UNPLACED: 1" in out, out


# ---------------------------------------------------------------------------
# 2 — the boundary row was reported absent by a comparison that never ran
# ---------------------------------------------------------------------------

def _boundary(sha: str):
    return rg.Boundary(
        state=rg.BOUNDARY_RESOLVED, tag={"name": "v0.38.0", "sha": sha},
        instant=rg.parse_instant("2026-08-01T00:00:00Z"), sha=sha,
        stamp="2026-08-01T00:00:00+00:00", branch_ref="refs/heads/master",
        notes=[], sources=[], refusal="")


def test_split_tagged_commit_says_whether_it_compared_anything() -> None:
    _rest, _tagged, compared = rg.split_tagged_commit([PLACED], "a" * 40)
    assert compared is True
    _rest, tagged, compared = rg.split_tagged_commit([PLACED], "")
    assert tagged is None
    assert compared is False, "no boundary sha means no comparison ran"


def test_an_unmeasured_boundary_row_is_not_reported_as_absent() -> None:
    _kept, lines, _code = rg.assess(
        rows=[PLACED], boundary=_boundary(""), per_page=100, fetched=1,
        repo_targeted=True, changelog_dir=str(Path(__file__).parent / "nope"))
    text = "|".join(lines)
    assert "boundary PR: NOT COMPARED" in text, text
    assert "boundary PR: none" not in text, text


def test_a_measured_absence_still_says_none() -> None:
    """The third state must not swallow the two that were already right."""
    _kept, lines, _code = rg.assess(
        rows=[PLACED], boundary=_boundary("b" * 40), per_page=100, fetched=1,
        repo_targeted=True, changelog_dir=str(Path(__file__).parent / "nope"))
    text = "|".join(lines)
    assert "boundary PR: none" in text, text
