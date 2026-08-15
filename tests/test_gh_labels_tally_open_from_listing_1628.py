"""#1628 — the burn-down's open column must not cost a search per label.

`gh-labels:tally=cohort-` is the designated source for a number
`/oss:manager` orders reported every tick. On 2026-08-13 it refused at 15
labels; on 2026-08-14, past the escape hatch at 16, it returned **34 of 34 cells
UNKNOWN** — the family cost `2N + 2` search calls against an API allowing 30 a
minute, and the family grows by one label per release. There is no future state
of the repository in which the old query plan starts working again.

The open column never needed a search. `fetch_open_issue_rows` was already
reading the whole open set on every tally call — the multi-label line printed
`all 74 open issues` on the very run where every per-label cell came back `?` —
and the per-label open counts are a client-side group-by over exactly that data.
So the open column is computed from the listing, and only the `closed` half,
which enumerating would render as a floor, still costs a search per label.

**What this file exists to pin is the failure mode, not the happy path.** A test
that passes because the search API happened to answer proves nothing here: the
defect *is* the search API not answering. So the load-bearing case is
`search_rc=1` — every search refused, the listing fine — where the open column
must be fully populated with real numbers while the closed column reads `?`.
Under the old plan that board was entirely `?`.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


labels = _load("presets/github/labels.py", "github_labels_1628")

REPO = "Digital-Process-Tools/claude-supertool"

LABELS = [
    {"name": "cohort-1", "description": ""},
    {"name": "cohort-2", "description": ""},
    {"name": "cohort-3", "description": ""},
    {"name": "bug", "description": ""},
]

# open: cohort-1 x2, cohort-2 x1, cohort-3 x0, none x2.  closed: cohort-1 x1,
# cohort-3 x1, none x1.  The two columns disagree on purpose, so a render that
# filled one of them from the other would be visible rather than plausible.
ISSUES = [
    {"number": 1, "state": "open", "labels": ["cohort-1", "bug"]},
    {"number": 2, "state": "open", "labels": ["cohort-1"]},
    {"number": 3, "state": "open", "labels": ["cohort-2"]},
    {"number": 4, "state": "open", "labels": ["bug"]},
    {"number": 5, "state": "open", "labels": []},
    {"number": 6, "state": "closed", "labels": ["cohort-1"]},
    {"number": 7, "state": "closed", "labels": ["cohort-3"]},
    {"number": 8, "state": "closed", "labels": []},
]


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _matches(issue: dict, q: str) -> bool:
    if "is:open" in q and issue["state"] != "open":
        return False
    if "is:closed" in q and issue["state"] != "closed":
        return False
    for name in re.findall(r'(?<!-)label:"([^"]+)"', q):
        if name not in issue["labels"]:
            return False
    for name in re.findall(r'-label:"([^"]+)"', q):
        if name in issue["labels"]:
            return False
    return True


class _Gh:
    """`gh`, with the two halves failable independently.

    `search_rc` is the whole point of the file: the search API is the half that
    rate-limits, and the enumeration is the half that does not.
    """

    def __init__(self, label_rows=None, search_rc: int = 0,
                 issue_rows=None, issues_rc: int = 0) -> None:
        self.label_rows = LABELS if label_rows is None else label_rows
        self.issue_rows = ISSUES if issue_rows is None else issue_rows
        self.search_rc = search_rc
        self.issues_rc = issues_rc
        self.queries: list[str] = []

    def __call__(self, argv, *a, **kw):
        argv = list(argv)
        joined = " ".join(argv)
        if "repo view" in joined:
            return _Completed(json.dumps({"nameWithOwner": REPO}))
        if "search/issues" in joined:
            q = ""
            for i, tok in enumerate(argv):
                if tok.startswith("q=") and argv[i - 1] == "-f":
                    q = tok[2:]
            self.queries.append(q)
            if self.search_rc:
                return _Completed("", returncode=self.search_rc, stderr="403")
            return _Completed(str(sum(1 for r in self.issue_rows
                                      if _matches(r, q))))
        if "issue list" in joined:
            if self.issues_rc:
                return _Completed("", returncode=self.issues_rc, stderr="500")
            limit = len(self.issue_rows)
            for i, tok in enumerate(argv):
                if tok == "--limit":
                    limit = int(argv[i + 1])
            rows = [r for r in self.issue_rows if r["state"] == "open"][:limit]
            return _Completed(json.dumps([
                {"number": r["number"],
                 "labels": [{"name": n} for n in r["labels"]]} for r in rows]))
        if "labels" in joined:
            return _Completed(json.dumps(self.label_rows))
        return _Completed("", returncode=1, stderr="404")


def _render(monkeypatch, capsys, argv: list[str], gh: _Gh) -> str:
    monkeypatch.setattr(labels.subprocess, "run", gh)
    monkeypatch.setattr(sys, "argv", ["labels.py", *argv])
    labels.main()
    return capsys.readouterr().out


# ---------------------------------------------------------------------------
# the query plan
# ---------------------------------------------------------------------------

def test_the_open_column_costs_no_search_at_all(monkeypatch, capsys) -> None:
    """One search per label, for `closed` only, plus one for the NONE bucket.

    `2N + 2` is what walked the family into the limiter. Asserting the absence
    of `is:open` rather than only a call count is what makes this a statement
    about the plan and not about this fixture's label list.
    """
    gh = _Gh()
    _render(monkeypatch, capsys, ["tally=cohort-"], gh)
    assert gh.queries, "it issued no searches at all"
    assert all("is:open" not in q for q in gh.queries), gh.queries
    assert len(gh.queries) == 4, gh.queries  # 3 labels closed + NONE closed


def test_the_open_column_survives_a_search_half_that_cannot_answer(
        monkeypatch, capsys) -> None:
    """The reported failure, exactly: every search refused, the listing fine.

    On 2026-08-14 this board came back 34 of 34 `?`. The number the maintainer
    is required to report every tick is the open column, and it is derivable
    without asking the half that rate-limits.
    """
    out = _render(monkeypatch, capsys, ["tally=cohort-"], _Gh(search_rc=1))
    assert re.search(r"cohort-1\s+2\s+\?\s+\?", out), out
    assert re.search(r"cohort-2\s+1\s+\?\s+\?", out), out
    assert re.search(r"cohort-3\s+0\s+\?\s+\?", out), out
    # And the NONE row, which is the one the escape hatch dropped first.
    assert re.search(r"no cohort- label\s+2\s+\?\s+\?", out), out


def test_a_dead_search_half_still_says_which_cells_it_could_not_fill(
        monkeypatch, capsys) -> None:
    """Three states, not two. A half-filled board that does not announce the
    half it lost is a partial read in the shape of a complete one."""
    out = _render(monkeypatch, capsys, ["tally=cohort-"], _Gh(search_rc=1))
    assert "UNKNOWN" in out
    assert "4 of 4" in out, out


# ---------------------------------------------------------------------------
# the open column's own three states
# ---------------------------------------------------------------------------

def test_an_unreadable_listing_leaves_the_open_column_unknown_not_zero(
        monkeypatch, capsys) -> None:
    """The mirror case. A `0` open cell would read as a finished cohort."""
    out = _render(monkeypatch, capsys, ["tally=cohort-"], _Gh(issues_rc=1))
    assert re.search(r"cohort-1\s+\?", out), out
    assert not re.search(r"cohort-1\s+0\s", out), out


def test_a_capped_listing_makes_the_open_column_a_floor(
        monkeypatch, capsys) -> None:
    """The cost of moving the column off search: an enumeration has a cap and
    a search does not. A floor rendered as an exact count is the same defect
    one layer along, so `>=` has to reach the cell and the sum it feeds."""
    monkeypatch.setenv("GH_LABELS_ISSUE_CAP", "2")
    out = _render(monkeypatch, capsys, ["tally=cohort-"], _Gh())
    assert re.search(r"cohort-1\s+>=2\s+1\s+>=3", out), out
    assert "GH_LABELS_ISSUE_CAP" in out, out


# ---------------------------------------------------------------------------
# the ceiling
# ---------------------------------------------------------------------------

def test_sixteen_cohorts_answer_by_default(monkeypatch, capsys) -> None:
    """The filed defect. 16 labels refused outright at the old 14 ceiling, and
    answered nothing past it — one search call per label is what buys the
    default invocation back."""
    rows = [{"name": f"cohort-{i}", "description": ""} for i in range(1, 17)]
    gh = _Gh(label_rows=rows)
    out = _render(monkeypatch, capsys, ["tally=cohort-"], gh)
    assert "GH_LABELS_TALLY_MAX" not in out, out
    assert "no cohort- label" in out, out


def test_the_refusal_still_fires_on_a_family_it_cannot_afford(
        monkeypatch, capsys) -> None:
    """Halving the per-label cost is not the same as removing the bound. Past
    it the honest answer is still a refusal naming the knob."""
    rows = [{"name": f"cohort-{i}", "description": ""} for i in range(60)]
    gh = _Gh(label_rows=rows)
    out = _render(monkeypatch, capsys, ["tally=cohort-"], gh)
    assert "GH_LABELS_TALLY_MAX" in out
    assert not gh.queries, "it queried anyway"


def test_the_refusal_quotes_the_cost_it_actually_pays(
        monkeypatch, capsys) -> None:
    """The message shows its working, and the working must be the current plan.
    A refusal quoting `2 per label` after the second call was deleted is a
    number nobody can check against the code that produced it."""
    rows = [{"name": f"cohort-{i}", "description": ""} for i in range(60)]
    out = _render(monkeypatch, capsys, ["tally=cohort-"], _Gh(label_rows=rows))
    assert "61 search calls" in out, out
