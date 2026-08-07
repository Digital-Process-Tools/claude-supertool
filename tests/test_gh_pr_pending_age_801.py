"""`gh-pr`'s tally must age its pending set (#801).

`18 total: 16 passed, 0 failed, 2 pending` is byte-identical whether the two
legs were queued ninety seconds ago and are about to run, or were queued an
hour ago behind a starved runner pool. Two opposite readings, one rendering —
this repository's house defect, pointed at the merge gate.

**Which age, and why this one.** Three candidates were available from the
payload `gh pr view` already returns, and they answer different questions:

* *run age* — how long CI has been going. Diluted by every leg that already
  finished; a three-hour-old run with a leg that started thirty seconds ago
  reads as alarming and is not.
* *time since the last leg changed state* — attractive, and actively wrong in
  the exact case this issue was filed from. When sixteen legs finished forty
  minutes ago and two have been queued ever since, the last state change was
  forty minutes ago — so a perfectly normal queue renders identically to a
  dead one, and renders as *dead*. It trades the ambiguous line for a
  confidently misleading one.
* *the oldest still-pending leg* — scoped to the thing the reader is waiting
  for, and nothing else. It is what this file pins.

None of the three *decides* queued-versus-wedged, and the op does not try to:
#801 is explicit that a `STALLED` verdict would be #750 again. The number is
reported and the reader compares it against a matrix duration they already
know. The tool knows the clock, not the intent.

Third state, per `docs/validators.md` §"Declining instead of guessing": a
pending leg whose start time cannot be established must make the age say
UNKNOWN, not silently drop out of the `oldest` computation — a leg omitted
from the maximum makes the age *younger*, which is the reassuring direction
and therefore the dangerous one.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent.parent

PRESET = ROOT / "presets" / "github" / "pr.py"
_spec = importlib.util.spec_from_file_location("github_pr_801", PRESET)
assert _spec is not None and _spec.loader is not None
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)

sys.path.insert(0, str(ROOT / "presets"))
import _checks  # noqa: E402

# gh renders "no timestamp" as a zero time rather than null. Parsing it as a
# real instant yields an age of roughly two thousand years, which is not a
# finding about CI.
GH_ZERO = "0001-01-01T00:00:00Z"


def _iso(minutes_ago: float) -> str:
    stamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# the unit: the age itself
# --------------------------------------------------------------------------

def test_no_pending_legs_adds_nothing() -> None:
    """A settled run must not grow a note."""
    assert _checks.pending_disclosure([]) == ("", [])


def test_oldest_pending_leg_is_the_age() -> None:
    note, lines = _checks.pending_disclosure([_iso(4.5), _iso(41.5), _iso(12.0)])
    assert "41m" in note, (
        f"the oldest pending leg is 41m old and the note does not say so: {note!r}"
    )
    assert "4m" not in note and "12m" not in note, (
        f"the note reports a leg that is not the oldest: {note!r}"
    )
    assert lines == [], f"nothing was unreadable, so nothing to disclose: {lines!r}"


def test_the_note_cannot_corrupt_the_term_arithmetic() -> None:
    """#454's parser reads `k <label>` terms out of the rendered line.

    A note carrying `<digits> <lowercase>` before a comma would be read back
    as a *term* and silently break the sum that is the whole point of that
    module. Everything with a digit or a comma belongs on a disclosure line.
    """
    for stamps in ([_iso(41.5), GH_ZERO, ""], [GH_ZERO, GH_ZERO], [_iso(3)]):
        note, _lines = _checks.pending_disclosure(stamps)
        assert "," not in note, f"a comma in the note splits a term: {note!r}"
        assert not re.search(r"\d+ [a-z]", note), (
            f"'<digits> <lowercase>' in the note parses as a tally term: {note!r}")


@pytest.mark.parametrize("bad", ["", None, GH_ZERO, "not-a-timestamp"],
                         ids=["empty", "null", "gh-zero-time", "garbage"])
def test_every_pending_leg_unreadable_declines(bad) -> None:
    note, lines = _checks.pending_disclosure([bad, bad])
    assert "UNKNOWN" in note, (
        f"no pending leg carries a usable start time, so the age is not "
        f"establishable and must say so. Got: {note!r}"
    )
    assert lines, "the decline is not explained anywhere"
    # The specific way this would go wrong quietly: the zero-time sentinel
    # parsed as a real instant, yielding a confident two-thousand-year age.
    assert not re.search(r"\d", note), (
        f"nothing here supports a number, and one is printed: {note!r}")


def test_some_unreadable_legs_are_disclosed_not_dropped() -> None:
    """The dangerous direction: dropping a leg makes the age *younger*."""
    note, lines = _checks.pending_disclosure([_iso(41.5), GH_ZERO, ""])
    assert "41m" in note, note
    body = " ".join(lines)
    assert "UNKNOWN" in body, (
        "two of three pending legs have no start time. Reporting only the "
        f"one that does, with no disclosure, understates the wait: {lines!r}"
    )
    assert "2 of 3" in body, f"the disclosure does not say how many: {lines!r}"
    assert "floor" in body, (
        f"the disclosure does not say the age is a lower bound: {lines!r}")


def test_a_future_timestamp_does_not_go_negative() -> None:
    """Clock skew between GitHub and the caller is not a negative age."""
    note, _lines = _checks.pending_disclosure([_iso(-5.0)])
    assert "-" not in note, note


# --------------------------------------------------------------------------
# the wiring: the age has to reach the line a maintainer reads
# --------------------------------------------------------------------------

def _rollup(legs: list[tuple[str, str, str]]) -> list[dict]:
    """`[(name, status, startedAt)]` -> a `statusCheckRollup` payload."""
    return [
        {
            "__typename": "CheckRun",
            "name": name,
            "status": status,
            "conclusion": "SUCCESS" if status == "COMPLETED" else "",
            "completedAt": GH_ZERO,
            "startedAt": started,
            "detailsUrl": (
                "https://github.com/o/r/actions/runs/1/job/"
                f"{900 + i}"
            ),
        }
        for i, (name, status, started) in enumerate(legs)
    ]


def _render(monkeypatch, capsys, legs: list[tuple[str, str, str]],
            slim: bool) -> str:
    payload = {
        "number": 798,
        "title": "t",
        "state": "OPEN",
        "author": {"login": "a"},
        "headRefName": "fix/798",
        "baseRefName": "master",
        "labels": [],
        "milestone": None,
        "reviewDecision": None,
        "reviews": [],
        "mergeCommit": None,
        "mergeable": "MERGEABLE",
        "isDraft": False,
        "url": "https://github.com/o/r/pull/798",
        "body": "",
        "comments": [],
        "additions": 1,
        "deletions": 0,
        "changedFiles": 1,
        "assignees": [],
        "createdAt": _iso(600),
        "updatedAt": _iso(1),
        "headRefOid": "0" * 40,
        "statusCheckRollup": _rollup(legs),
    }
    monkeypatch.setattr(
        pr, "_gh",
        lambda *a, **k: SimpleNamespace(
            returncode=0, stdout=json.dumps(payload), stderr=""),
    )
    # Reconciliation is #724/#804's concern and buys network calls this test
    # is not about; it appends its own marker to the same line.
    monkeypatch.setattr(pr, "_reconcile_checks", lambda d: ("", []))
    monkeypatch.setattr(pr, "_local_branch_check", lambda s: "")
    monkeypatch.setattr(pr, "_fetch_review_threads", lambda *a, **k: [])
    argv = ["pr.py", "798"] + (["status"] if slim else [])
    monkeypatch.setattr(sys, "argv", argv)
    assert pr.main() == 0
    return capsys.readouterr().out


def _checks_line(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("checks: ") or line.startswith("Checks: "):
            return line
    raise AssertionError(f"no checks line in:\n{out}")


# Built per call, never at import. Held as module constants these drifted with
# the suite's own wall clock: `_iso(2.5)` frozen at collection is 9m old by the
# time a seven-minute run reaches the assertion, and the test failed in the
# full suite while passing on its own. A fixture whose value depends on how
# long the suite takes is not a fixture.
def _starved() -> list[tuple[str, str, str]]:
    """Sixteen minutes of ubuntu done, two macOS legs queued 41m ago."""
    return [
        ("pytest (ubuntu-latest, 3.9)", "COMPLETED", _iso(70)),
        ("pytest (ubuntu-latest, 3.12)", "COMPLETED", _iso(70)),
        ("pytest (macos-latest, 3.9)", "QUEUED", _iso(41.5)),
        ("pytest (macos-latest, 3.12)", "QUEUED", _iso(41.5)),
    ]


def _fresh() -> list[tuple[str, str, str]]:
    """The same tally, two and a half minutes old."""
    return [
        ("pytest (ubuntu-latest, 3.9)", "COMPLETED", _iso(3)),
        ("pytest (ubuntu-latest, 3.12)", "COMPLETED", _iso(3)),
        ("pytest (macos-latest, 3.9)", "QUEUED", _iso(2.5)),
        ("pytest (macos-latest, 3.12)", "QUEUED", _iso(2.5)),
    ]


@pytest.mark.parametrize("slim", [True, False], ids=["status", "full"])
def test_the_two_situations_no_longer_render_identically(
        monkeypatch, capsys, slim) -> None:
    """#801's whole complaint, as one assertion."""
    wedged = _checks_line(_render(monkeypatch, capsys, _starved(), slim))
    moving = _checks_line(_render(monkeypatch, capsys, _fresh(), slim))

    # The control: both really are the same tally.
    for line in (wedged, moving):
        assert "4 total: 2 passed, 0 failed, 2 pending" in line, line

    assert wedged != moving, (
        "a 41-minute-old pending set and a 2-minute-old one render "
        f"byte-identically:\n  {wedged!r}"
    )
    assert "41m" in wedged, wedged
    assert "2m" in moving, moving


@pytest.mark.parametrize("slim", [True, False], ids=["status", "full"])
def test_the_age_does_not_become_a_verdict(monkeypatch, capsys, slim) -> None:
    """#801 forbids a staleness alarm — #750's 0-for-12 false-positive record."""
    line = _checks_line(_render(monkeypatch, capsys, _starved(), slim))
    for word in ("STALLED", "STUCK", "WEDGED"):
        assert word not in line.upper(), (
            f"{word} is a conclusion the clock cannot support: {line!r}")


@pytest.mark.parametrize("slim", [True, False], ids=["status", "full"])
def test_a_green_run_gains_no_parenthetical(monkeypatch, capsys, slim) -> None:
    """The control: no pending legs, no age, no new noise."""
    green = [("a", "COMPLETED", _iso(9)), ("b", "COMPLETED", _iso(9))]
    line = _checks_line(_render(monkeypatch, capsys, green, slim))
    assert line.rstrip().endswith("2 total: 2 passed, 0 failed, 0 pending"), line


@pytest.mark.parametrize("slim", [True, False], ids=["status", "full"])
def test_unreadable_pending_stamps_reach_the_line_as_unknown(
        monkeypatch, capsys, slim) -> None:
    legs = [("a", "COMPLETED", _iso(9)), ("b", "QUEUED", GH_ZERO)]
    line = _checks_line(_render(monkeypatch, capsys, legs, slim))
    assert "UNKNOWN" in line, (
        f"the pending leg has no usable start time and the line is silent "
        f"about it: {line!r}")
