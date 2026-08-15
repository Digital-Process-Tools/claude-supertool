"""#1084 — the cohort burn-down was thirty characters of jq, every tick.

`/oss:manager` must report the rolling-cohort count and its delta on
every tick, and the whole mechanism is one comparison: *is each cohort smaller
than the last?* That is a group-by over one label family, and no op did it, so
it came out as a `gh issue list --json labels -q 'group_by'` pipeline rewritten
from scratch each session — and unwritable at all by a fresh agent.

`gh-labels` (#998) already had the data and most of the honesty: the label
vocabulary, grouped by inferred prefix, with an open-issue count per label in
three states. Two things were missing and both carry the decision.

**The NONE bucket.** `gh-labels` counts labels that exist. The number the
cohort rule turns on is how many open issues carry *no* label of the family —
the ones that escaped the freeze — and a per-label listing cannot show it by
construction. Same reason `gh-issues:nomilestone` is a flag and not an absence.

**The closed half.** `cohort-1 frozen 72 open 48` needs both, and a burn-down
whose denominator is missing sends the reader straight back to jq. Open counts
alone would have been an op that does not answer the question it was filed for.

The trap this file mostly guards is the arithmetic. `frozen` is a sum, so a
cell that could not be read must poison the sum rather than be treated as
zero — a partial read rendering as a total is this repository's most-filed
defect, and a burn-down denominator is exactly the number a human is asked to
trust over weeks.
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


labels = _load("presets/github/labels.py", "github_labels_1084")

REPO = "Digital-Process-Tools/claude-supertool"

LABELS = [
    {"name": "cohort-1", "description": "closed set as of 2026-08-07"},
    {"name": "cohort-2", "description": ""},
    {"name": "cohort-3", "description": ""},
    {"name": "priority-high", "description": ""},
    {"name": "priority-low", "description": ""},
    {"name": "bug", "description": ""},
]

# `state` is what the search API filters on; `labels` is the whole set.
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
    """`gh`, answering the search API the way the search API answers."""

    def __init__(self, label_rows=None, search_rc: int = 0,
                 issue_rows=None, issues_rc: int = 0) -> None:
        self.label_rows = LABELS if label_rows is None else label_rows
        self.issue_rows = ISSUES if issue_rows is None else issue_rows
        self.search_rc = search_rc
        self.issues_rc = issues_rc
        self.queries: list[str] = []
        self.calls: list[list[str]] = []

    def __call__(self, argv, *a, **kw):
        argv = list(argv)
        self.calls.append(argv)
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
            n = sum(1 for row in self.issue_rows if _matches(row, q))
            return _Completed(str(n))
        # Order matters: `gh issue list --json labels` carries the word too.
        if "issue list" in joined:
            if self.issues_rc:
                return _Completed("", returncode=self.issues_rc, stderr="500")
            return _Completed(json.dumps([
                {"number": r["number"],
                 "labels": [{"name": n} for n in r["labels"]]}
                for r in self.issue_rows if r["state"] == "open"]))
        if "labels" in joined:
            return _Completed(json.dumps(self.label_rows))
        return _Completed("", returncode=1, stderr="404")


def _render(monkeypatch, capsys, argv: list[str], gh: _Gh) -> str:
    monkeypatch.setattr(labels.subprocess, "run", gh)
    monkeypatch.setattr(sys, "argv", ["labels.py", *argv])
    labels.main()
    return capsys.readouterr().out


# ---------------------------------------------------------------------------
# the argument
# ---------------------------------------------------------------------------

def test_no_argument_is_still_the_vocabulary() -> None:
    assert labels.parse_args([]) == ("", "")


def test_the_prefix_is_a_parameter() -> None:
    assert labels.parse_args(["tally=cohort-"]) == ("cohort-", "")


def test_a_colon_spelled_family_is_accepted() -> None:
    """`claude-remember` spells it `priority:high`. A prefix hardcoded to this
    repo's `-` would silently answer for a family that does not exist."""
    assert labels.parse_args(["tally=priority:"]) == ("priority:", "")


def test_an_empty_prefix_is_refused() -> None:
    prefix, err = labels.parse_args(["tally="])
    assert prefix == "" and err.startswith("ERROR")


def test_an_unrecognised_token_is_refused_not_dropped() -> None:
    """A token nobody applied must not produce the full vocabulary as if it
    had been understood — `gh-issues`' rule, for the same reason."""
    prefix, err = labels.parse_args(["by=cohort-"])
    assert prefix == "" and err.startswith("ERROR")


# ---------------------------------------------------------------------------
# the queries
# ---------------------------------------------------------------------------

def test_every_query_excludes_pull_requests(monkeypatch, capsys) -> None:
    gh = _Gh()
    _render(monkeypatch, capsys, ["tally=cohort-"], gh)
    assert gh.queries
    assert all("is:issue" in q for q in gh.queries)


def test_open_and_closed_are_asked_for_separately(monkeypatch, capsys) -> None:
    """Retargeted by #1628, not deleted. The two columns are still two
    separate questions; what changed is that `open` stopped being one of the
    searches. `2N + 2` calls against a 30-a-minute API returned a board of 34
    `?` at 16 cohorts, and the listing that answers `open` was already being
    fetched on the same run. Asserting the open search is *absent* is what
    stops the old plan creeping back one filter at a time.
    """
    gh = _Gh()
    out = _render(monkeypatch, capsys, ["tally=cohort-"], gh)
    for name in ("cohort-1", "cohort-2", "cohort-3"):
        assert any(f'label:"{name}"' in q and "is:closed" in q
                   for q in gh.queries), name
    assert all("is:open" not in q for q in gh.queries), gh.queries
    # The column is still filled, from the issue listing: open 2, closed 1.
    assert re.search(r"cohort-1\s+2\s+1\s+3", out), out


def test_the_none_bucket_negates_every_member(monkeypatch, capsys) -> None:
    gh = _Gh()
    _render(monkeypatch, capsys, ["tally=cohort-"], gh)
    negating = [q for q in gh.queries if '-label:"cohort-1"' in q]
    assert negating, "no query asked for issues carrying none of the family"
    for q in negating:
        for name in ("cohort-1", "cohort-2", "cohort-3"):
            assert f'-label:"{name}"' in q


# ---------------------------------------------------------------------------
# the arithmetic
# ---------------------------------------------------------------------------

def test_a_label_row_carries_open_closed_and_their_sum(
        monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, ["tally=cohort-"], _Gh())
    row = [ln for ln in out.splitlines() if "cohort-1" in ln and "frozen" not in ln]
    assert row, out
    assert re.search(r"cohort-1\s+2\s+1\s+3", row[0]), row[0]


def test_the_none_row_is_counted_and_named(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, ["tally=cohort-"], _Gh())
    none_row = [ln for ln in out.splitlines() if "no cohort-" in ln]
    assert none_row, out
    # open issues 4 and 5 carry no cohort label; closed issue 8 carries none.
    assert re.search(r"\s2\s+1\s+3", none_row[0]), none_row[0]


def test_an_unreadable_count_is_never_zero_and_poisons_the_sum(
        monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, ["tally=cohort-"], _Gh(search_rc=1))
    assert "?" in out
    assert not re.search(r"cohort-1\s+0\s", out), (
        "a search that did not answer rendered as a count of zero — 'I did "
        "not look' and 'nobody is in this cohort' are opposite facts")
    assert "UNKNOWN" in out


def test_a_family_that_does_not_exist_says_so(monkeypatch, capsys) -> None:
    """The cross-repo case: `lane-` does not exist in `claude-remember`. An
    empty family must read as *no labels in this family*, never as a board
    where every issue is in the NONE bucket."""
    out = _render(monkeypatch, capsys, ["tally=lane-"], _Gh())
    assert "no labels" in out.lower()
    assert "no lane- label" not in out


def test_an_issue_in_two_cohorts_is_a_filing_error_not_a_row(
        monkeypatch, capsys) -> None:
    rows = list(ISSUES)
    rows[2] = {"number": 3, "state": "open", "labels": ["cohort-2", "cohort-3"]}
    out = _render(monkeypatch, capsys, ["tally=cohort-"], _Gh(issue_rows=rows))
    assert "#3" in out
    assert "more than one" in out.lower() or "two labels" in out.lower()


def test_a_clean_family_says_the_check_ran(monkeypatch, capsys) -> None:
    """The complement matters: silence about the multi-label check reads as
    'no offenders' and as 'not checked' identically."""
    out = _render(monkeypatch, capsys, ["tally=cohort-"], _Gh())
    # Not just the phrase: both branches say "more than one", so matching on it
    # alone passes whether the clean case is reported or the two branches are
    # swapped. The word that distinguishes them is the one to assert.
    assert "none of" in out.lower()
    assert "more than one" in out.lower()
    assert "#" not in out.split("Multi-label:")[1]


def test_an_unreadable_issue_list_does_not_claim_a_clean_family(
        monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, ["tally=cohort-"],
                  _Gh(issues_rc=1))
    assert "UNKNOWN" in out


# ---------------------------------------------------------------------------
# the op it was added to
# ---------------------------------------------------------------------------

def test_the_plain_vocabulary_render_is_untouched(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, [], _Gh())
    assert "# Labels" in out
    assert "frozen" not in out


def test_the_tally_is_not_the_vocabulary(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, ["tally=cohort-"], _Gh())
    assert "priority-high" not in out, (
        "the tally answers about one family; printing the whole vocabulary "
        "under it is the render the caller did not ask for")

def test_a_family_too_large_to_query_refuses(monkeypatch, capsys) -> None:
    """One search call per label since #1628 — `closed` only — and the search
    API is still rate limited. Past a bound the honest answer is a refusal
    naming the knob, not a board where half the cells are `?` because the
    limiter cut in halfway down. The bound moved 14 -> 24; 40 labels is past
    both, so this test says the same thing it always did."""
    rows = [{"name": f"cohort-{i}", "description": ""} for i in range(40)]
    gh = _Gh(label_rows=rows)
    out = _render(monkeypatch, capsys, ["tally=cohort-"], gh)
    assert "GH_LABELS_TALLY_MAX" in out
    assert not gh.queries, "it queried anyway"


def test_a_label_name_cannot_break_out_of_the_query(monkeypatch, capsys) -> None:
    """Label names are remote text and land inside a quoted search term."""
    rows = [{"name": 'cohort-1" OR is:pr', "description": ""},
            {"name": "cohort-2", "description": ""}]
    gh = _Gh(label_rows=rows)
    _render(monkeypatch, capsys, ["tally=cohort-"], gh)
    assert all("is:pr" not in q for q in gh.queries), gh.queries
