"""#1439 — `iids` turned every client-side filter off, decline included.

Measured on the live board before the fix:

    $ supertool 'gh-issues:nomilestone,per=100,iids'       -> 95 ids
    $ supertool 'gh-issues:milestone=v0.36.0,per=100,iids' ->  42 ids
    intersection: 42 — every milestoned issue also reported as unmilestoned
    $ supertool 'gh-issues:nomilestone,per=100'
    53 issue(s) | nomilestone excluded 42 of 95 fetched

**The mechanism is not the one the issue body proposed.** The body's hypothesis
was that the `iids` projection drops the `milestone` field before the filter
reads it. It does not: both shapes come from the same `gh issue list --json`
call with the same `_LIST_FIELDS`, and the field is present on every row. The
cause is **ordering** — the `numbers_only` early return sat *above* the whole
client-side filter block, so `external`, `stale` and `nomilestone` were all
inert under `iids`, and so were their declines. Nothing was dropped; the filter
simply never ran.

That also settles the class question the issue asks: it is not per-filter, it is
every flag in the block, and the sibling op had already got it right. `gl-mrs`
applies `failed` *before* its own `iids_only` return, and sets
`show_pipe = failed_only or "nopipe" not in flags` so a filter that needs
enrichment pays for it. `gh-issues` now has the same shape.

Three properties, and the third is the one the issue was really about:

* a client-side filter applies to the number list exactly as it applies to the
  board;
* a filter whose field is unknown **declines** under `iids` too — an id feed is
  the shape most likely to be consumed by a script rather than read, so it is
  the shape least able to carry a wrong answer;
* the narrowing note rides the id feed as a `#` comment, because the non-`iids`
  render states `nomilestone excluded 42 of 95 fetched` and the id feed stated
  nothing at all — there was no line to disagree with.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


issues = _load("presets/github/issues.py", "github_issues_1439")

REPO = "https://github.com/Digital-Process-Tools/claude-supertool"


def _row(number: int, milestone: object = None, **over: Any) -> dict:
    row = {
        "number": number,
        "title": f"issue {number}",
        "state": "OPEN",
        "author": {"login": "someone"},
        "labels": [],
        "assignees": [],
        "milestone": milestone,
        "createdAt": "2026-08-01T00:00:00Z",
        "updatedAt": "2026-08-01T00:00:00Z",
        # One comment, not zero: `_annotate` can settle `_stale` from the list
        # data alone when an issue has no comments at all, so a zero-comment
        # fixture makes the `stale` decline unreachable and the test vacuous.
        "comments": [{"createdAt": "2026-08-02T00:00:00Z"}],
        "url": f"{REPO}/issues/{number}",
    }
    row.update(over)
    return row


def _board(monkeypatch: Any, rows: list[dict], enrichment: object = "none"):
    """Stub `gh issue list`. `enrichment` mocks the GraphQL half."""
    calls: dict[str, int] = {"list": 0, "enrich": 0}

    def _run(argv, **kwargs):
        calls["list"] += 1
        return types.SimpleNamespace(
            returncode=0, stdout=json.dumps(rows), stderr="")

    monkeypatch.setattr(issues.subprocess, "run", _run)

    def _enrich(owner, name, numbers, chunk=20):
        calls["enrich"] += 1
        if enrichment == "none":
            return {}, "the enrichment call was not stubbed"
        return {n: dict(enrichment) for n in numbers}, None

    monkeypatch.setattr(issues, "_fetch_enrichment", _enrich)
    return calls


def _out(capsys: Any) -> tuple[list[str], str]:
    cap = capsys.readouterr()
    return cap.out.splitlines(), cap.err


def _numbers(lines: list[str]) -> list[int]:
    return [int(ln) for ln in lines if ln.strip().isdigit()]


# ---------------------------------------------------------------------------
# the reported pair
# ---------------------------------------------------------------------------

MIXED = [
    _row(1, milestone={"title": "v0.36.0"}),
    _row(2, milestone=None),
    _row(3, milestone={"title": "v0.36.0"}),
    _row(4, milestone=None),
]


def test_nomilestone_narrows_the_id_feed(monkeypatch: Any, capsys: Any) -> None:
    _board(monkeypatch, MIXED)
    assert issues.main_with_args("nomilestone,iids") == 0
    lines, _err = _out(capsys)
    assert _numbers(lines) == [2, 4], lines


def test_the_same_filter_gives_the_same_population_with_and_without_iids(
        monkeypatch: Any, capsys: Any) -> None:
    """The property the live reproduction measured: the two must not disagree."""
    _board(monkeypatch, MIXED)
    assert issues.main_with_args("nomilestone,iids") == 0
    ids, _ = _out(capsys)
    piped = set(_numbers(ids))

    _board(monkeypatch, MIXED)
    assert issues.main_with_args("nomilestone") == 0
    board, _ = _out(capsys)
    rendered = {n for n in (1, 2, 3, 4)
                if any(f"#{n}" in ln for ln in board)}
    assert piped == rendered, (sorted(piped), sorted(rendered), board)


def test_the_narrowing_note_rides_the_id_feed(
        monkeypatch: Any, capsys: Any) -> None:
    """The tell that was missing: no line to disagree with is why it passed."""
    _board(monkeypatch, MIXED)
    assert issues.main_with_args("nomilestone,iids") == 0
    lines, _err = _out(capsys)
    notes = [ln for ln in lines if ln.startswith("#")]
    assert any("nomilestone excluded 2 of 4 fetched" in ln for ln in notes), lines


# ---------------------------------------------------------------------------
# the decline, which `iids` must be able to carry
# ---------------------------------------------------------------------------

def test_an_unknown_milestone_declines_under_iids(
        monkeypatch: Any, capsys: Any) -> None:
    """A row whose `milestone` key never came back cannot be placed.

    Filtering it in reports a scheduled issue as unscheduled; filtering it out
    drops exactly the gap the query exists to find. The board already declined
    here and the id feed did not.
    """
    rows = [_row(1, milestone=None), dict(_row(2))]
    del rows[1]["milestone"]
    _board(monkeypatch, rows)
    assert issues.main_with_args("nomilestone,iids") == 1
    lines, err = _out(capsys)
    assert "cannot filter by nomilestone" in err, err
    assert _numbers(lines) == [], lines


@pytest.mark.parametrize("flag,field", [
    ("external", "author association"),
    ("stale", "body-edit time"),
])
def test_an_unenriched_field_declines_under_iids(
        monkeypatch: Any, capsys: Any, flag: str, field: str) -> None:
    """`nopipe` makes the field unknown by construction; the feed must not lie."""
    _board(monkeypatch, MIXED)
    assert issues.main_with_args(f"{flag},nopipe,iids") == 1
    lines, err = _out(capsys)
    assert f"cannot filter by {flag}" in err, err
    assert _numbers(lines) == [], lines


# ---------------------------------------------------------------------------
# a filter that needs enrichment pays for it — `gl-mrs`'s rule
# ---------------------------------------------------------------------------

def test_external_under_iids_buys_the_enrichment_it_needs(
        monkeypatch: Any, capsys: Any) -> None:
    calls = _board(monkeypatch, MIXED, enrichment={
        "authorAssociation": "NONE", "lastEditedAt": None,
        "timelineItems": {"nodes": []},
    })
    assert issues.main_with_args("external,iids") == 0
    lines, err = _out(capsys)
    assert calls["enrich"] == 1, (calls, err)
    assert _numbers(lines) == [1, 2, 3, 4], lines


def test_a_bare_id_feed_still_pays_for_no_enrichment_at_all(
        monkeypatch: Any, capsys: Any) -> None:
    """The control. `iids` alone is the cheap shape and must stay cheap."""
    calls = _board(monkeypatch, MIXED)
    assert issues.main_with_args("iids") == 0
    lines, _err = _out(capsys)
    assert calls["enrich"] == 0, calls
    assert _numbers(lines) == [1, 2, 3, 4], lines
