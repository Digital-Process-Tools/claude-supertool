"""#1024 — `N no longer open` is a claim the snapshot cannot support.

Both radar tiers compute the departed set as "in the previous snapshot, not in
`live`", and `live` is not the open population — it is the open population
*that matches this board's filter*. So five different histories land in one
branch and render as one sentence:

    merged                     -> "no longer open"  true
    closed unmerged            -> "no longer open"  true
    author reassigned          -> "no longer open"  FALSE, still open
    a selected-on label pulled -> "no longer open"  FALSE, still open
    pushed off the fetch page  -> "no longer open"  FALSE, still open

(A changed filter looks like a sixth and is not one: the snapshot is keyed by
filter, so widening one is a cold start with nothing to depart from.)

A maintainer reading `1 no longer open` concludes a PR landed and stops
tracking it. In three of those five rows the PR is open and still needs work,
so the board has told the reader the opposite of the truth using a line that is
doing exactly what it was written to do.

The snapshot genuinely cannot tell the five apart — it records what was in the
population last tick and nothing about why something left. So the fix is not a
better guess, it is the third state: say that the entry left this board, name
it, name the ways that can happen, and give the op that answers which. That
costs no extra call and cannot be wrong.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
WATCH_DIR = ROOT / "presets" / "watch"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tier = _module("radar_gh_prs_1024", WATCH_DIR / "tiers" / "gh_prs.py")
gl_tier = _module("radar_gl_mrs_1024", WATCH_DIR / "tiers" / "gl_mrs.py")


RUN_LEG = {"name": "pytest", "status": "IN_PROGRESS", "conclusion": None,
           "detailsUrl": "https://github.com/o/r/actions/runs/1/job/9"}
RED_LEG = {"name": "pytest (windows-latest, 3.12)", "status": "COMPLETED",
           "conclusion": "FAILURE",
           "detailsUrl": "https://github.com/o/r/actions/runs/1/job/9"}
GREEN_LEG = {"name": "pytest", "status": "COMPLETED", "conclusion": "SUCCESS",
             "detailsUrl": "https://github.com/o/r/actions/runs/1/job/9"}


def _pr(number: int, rollup, sha: str = "a" * 40, **kw) -> dict:
    row = {
        "number": number, "title": f"pr {number}", "state": "OPEN",
        "author": {"login": "me"}, "headRefName": f"fix/{number}",
        "baseRefName": "master", "headRefOid": sha, "labels": [],
        "isDraft": False, "mergeable": "MERGEABLE", "reviewDecision": "",
        "statusCheckRollup": rollup, "additions": 1, "deletions": 1,
        "changedFiles": 1, "updatedAt": "2026-08-07T10:00:00Z",
        "createdAt": "2026-08-07T09:00:00Z", "assignees": [],
        "url": f"https://github.com/o/r/pull/{number}",
    }
    row.update(kw)
    return row


def _mr(iid: int, status: str, **kw) -> dict:
    row = {
        "iid": iid, "title": f"mr {iid}", "source_branch": f"fix/{iid}",
        "target_branch": "master", "draft": False,
        "updated_at": "2026-08-07T10:00:00Z",
        "_pipeline": status, "_pipeline_id": str(100 + iid), "_pipeline_url": "",
        "_changes": 3, "_approved": True, "_approved_by": [],
        "_failed_jobs": [] if status != "failed" else ["test_unit"],
        "_enriched": True,
    }
    row.update(kw)
    return row


class _Result:
    def __init__(self, out: str = "", err: str = "", code: int = 0):
        self.stdout, self.stderr, self.returncode = out, err, code


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tier.transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tier.snapshot.transport, "STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def no_spawn(monkeypatch):
    monkeypatch.setattr(tier.dispatcher, "start_poller",
                        lambda *a, **k: pytest.fail("tier spawned a poller"))


@pytest.fixture(autouse=True)
def quiet_reconcile(monkeypatch):
    monkeypatch.setattr(tier, "_reconcile_one", lambda p: ("", []))


def _board(monkeypatch, rows, watched):
    # #2024: default_branch_report now also reports the standing poller's
    # own health as a third, independent value -- see its own docstring.
    monkeypatch.setattr(tier, "default_branch_report",
                        lambda *a, **k: ([], True, True))
    monkeypatch.setattr(tier, "repo_name",
                        lambda: "Digital-Process-Tools/claude-supertool")
    monkeypatch.setattr(tier, "watch_coverage", lambda: set(watched))
    monkeypatch.setattr(tier.subprocess, "run",
                        lambda cmd, *a, **k: _Result(json.dumps(rows)))
    lines, _ = tier.radar_report({"_arg": "", "_watch": lambda *a, **k: "alive"})
    return lines


_DEPARTED = re.compile(r"(\d+) left this board\b")


# ---------------------------------------------------------------------------
# the claim the board must stop making
# ---------------------------------------------------------------------------

def test_gh_prs_does_not_call_a_departure_no_longer_open(state_dir, monkeypatch):
    """`live` is filter-scoped, so "gone from `live`" is not "merged".

    The fixture is the reassignment case: #1013 is still open, it simply stopped
    matching `author=@me`. Nothing in the snapshot distinguishes it from a merge,
    which is precisely why the board must not name one of the two.
    """
    _board(monkeypatch, [_pr(1005, [RED_LEG]), _pr(1013, [RUN_LEG])],
           {"1005", "1013"})
    lines = _board(monkeypatch, [_pr(1005, [RED_LEG])], {"1005"})
    text = "\n".join(lines)

    # The board first. Without this the central claim of #1024 is carried by an
    # assertion a radar printing nothing satisfies — the #800 defect, in the
    # file that exists to fix #800's sibling.
    assert "#1005" in text, f"no board was printed at all.\n\n{text}"
    assert "no longer open" not in text, (
        "#1013 left the *filtered* population; it is still open and still needs "
        "work. A reader acting on this line stops tracking a live PR.\n\n" + text)


def test_gh_prs_names_the_departed_pr_and_what_it_cannot_tell(
        state_dir, monkeypatch):
    """A count a reader cannot resolve is not a disclosure, and a disclosure
    that does not say what it could not determine is still a guess."""
    _board(monkeypatch, [_pr(1005, [RED_LEG]), _pr(1013, [RUN_LEG])],
           {"1005", "1013"})
    lines = _board(monkeypatch, [_pr(1005, [RED_LEG])], {"1005"})
    text = "\n".join(lines)

    footer = [ln for ln in lines if " open" in ln and " | " in ln][-1]
    found = _DEPARTED.search(footer)
    assert found and found.group(1) == "1", (
        f"the departure is still counted in the footer, in words the snapshot "
        f"can back up.\n\n{text}")

    note = [ln for ln in lines if "left this board" in ln and "#1013" in ln]
    assert note, f"#1013 departed and was never named.\n\n{text}"
    joined = " ".join(note)
    for word in ("merged", "closed", "still open"):
        assert word in joined, (
            f"the note does not say {word!r} is one of the histories it cannot "
            f"tell apart, so the reader still has to assume one.\n\n{text}")
    assert "gh-pr:" in joined, (
        f"nothing on the board says how to find out which it was.\n\n{text}")


def test_a_departure_only_tick_is_not_reported_as_no_change(state_dir, monkeypatch):
    """A departure *is* a change, and it is the one the delta cannot re-print.

    Every remaining row is unchanged, so the whole board elides and the summary
    line takes the `no change` arm — while the footer on that same line counts a
    departure and a NOTE names it. `radar: no change` is the token this board is
    skimmed by, so the tick where something silently fell off the board is
    exactly the tick that announces nothing happened.
    """
    _board(monkeypatch, [_pr(1005, [RUN_LEG]), _pr(1013, [RUN_LEG])],
           {"1005", "1013"})
    lines = _board(monkeypatch, [_pr(1005, [RUN_LEG])], {"1005"})
    text = chr(10).join(lines)

    assert "left this board" in text, f"fixture drifted: {text}"
    assert not any(ln.startswith("radar: no change") for ln in lines), (
        "#1013 left the board on this tick and the summary line says nothing "
        f"changed.{chr(10)}{chr(10)}{text}")


def test_gl_mrs_departure_only_tick_is_not_reported_as_no_change(monkeypatch):
    mrs = [_mr(1, "running"), _mr(2, "running")]
    previous = {"mrs": {str(m["iid"]): gl_tier._snap_entry(m) for m in mrs}}
    lines = gl_tier.render([mrs[0]], {"1"}, [], {}, [], [], previous,
                           label="scope author=@me")
    text = chr(10).join(lines)

    assert "left this board" in text, f"fixture drifted: {text}"
    assert not any(ln.startswith("radar: no change") for ln in lines), text


def test_departed_identifiers_are_named_in_a_stable_order(state_dir, monkeypatch):
    """The cap at twelve makes *which* ids get named load-bearing.

    Snapshot entries are written in the order the API returned them, so an
    unsorted departed list truncates arbitrarily and differently between two
    runs over the same departures — a disclosure whose content depends on
    upstream ordering is one a reader cannot check.
    """
    first = [_pr(n, [RUN_LEG]) for n in (1300, 900, 1005, 1100)]
    _board(monkeypatch, first, {str(p["number"]) for p in first})
    lines = _board(monkeypatch, [_pr(1005, [RUN_LEG])], {"1005"})

    note = next(ln for ln in lines if "left this board" in ln)
    order = re.findall(r"#(\d+)", note)
    assert order == ["900", "1100", "1300"], note


def test_gh_prs_says_nothing_when_nothing_departed(state_dir, monkeypatch):
    """The absence of the line is the positive claim, exactly as for elision."""
    rows = [_pr(1005, [RED_LEG]), _pr(1013, [RUN_LEG])]
    _board(monkeypatch, rows, {"1005", "1013"})
    lines = _board(monkeypatch, rows, {"1005", "1013"})
    text = "\n".join(lines)
    # "The absence is the claim" only holds over a board that exists.
    assert "2 open" in text, f"no board was printed at all.\n\n{text}"
    assert "left this board" not in text, text
    assert "no longer open" not in text, text


# ---------------------------------------------------------------------------
# the sibling tier carries the same construct (#1022 did too)
# ---------------------------------------------------------------------------

def test_gl_mrs_does_not_call_a_departure_no_longer_open(monkeypatch):
    mrs = [_mr(1, "failed"), _mr(2, "running")]
    previous = {"mrs": {str(m["iid"]): gl_tier._snap_entry(m) for m in mrs}}

    lines = gl_tier.render([mrs[0]], {"1"}, [], {}, [], [], previous,
                           label="scope author=@me")
    text = "\n".join(lines)

    assert "no longer open" not in text, (
        "!2 left the filtered population; the snapshot cannot say it "
        f"merged.\n\n{text}")

    footer = [ln for ln in lines if " open" in ln and " | " in ln][-1]
    found = _DEPARTED.search(footer)
    assert found and found.group(1) == "1", f"{text}"

    note = [ln for ln in lines if "left this board" in ln and "!2" in ln]
    assert note, f"!2 departed and was never named.\n\n{text}"
    joined = " ".join(note)
    for word in ("merged", "closed", "still open"):
        assert word in joined, f"{word!r} missing from the note.\n\n{text}"
    assert "gl-mr:" in joined, text


def test_gl_mrs_says_nothing_when_nothing_departed(monkeypatch):
    mrs = [_mr(1, "failed"), _mr(2, "running")]
    previous = {"mrs": {str(m["iid"]): gl_tier._snap_entry(m) for m in mrs}}
    lines = gl_tier.render(mrs, {"1", "2"}, [], {}, [], [], previous,
                           label="scope author=@me")
    text = "\n".join(lines)
    assert "2 open" in text, f"no board was printed at all.\n\n{text}"
    assert "left this board" not in text, text
    assert "no longer open" not in text, text


# ---------------------------------------------------------------------------
# the fourth history the note had no arm for
# ---------------------------------------------------------------------------

def test_a_full_page_cannot_establish_a_departure(state_dir, monkeypatch):
    """`live_open_prs` fetches ONE page, `per_page` default 50, no pagination.

    So `previous - live` has a fourth member the note did not name: an entry
    pushed off page 1 by newer ones. It is open, it still matches the filter,
    and every one of the three histories offered is false about it. The reader
    who follows the note and runs the lookup sees it open and concludes the
    filter stopped matching — a second wrong conclusion, reached by doing
    exactly what the board said.
    """
    monkeypatch.setattr(tier.prs, "_get_config", lambda: {"per_page": 2})
    _board(monkeypatch, [_pr(900, [RUN_LEG]), _pr(1005, [RUN_LEG])],
           {"900", "1005"})
    # Two newer PRs open; the page cuts at 2 and #900 falls off it.
    lines = _board(monkeypatch, [_pr(1100, [RUN_LEG]), _pr(1005, [RUN_LEG])],
                   {"1100", "1005"})
    text = "\n".join(lines)

    assert "#1005" in text, f"no board was printed at all.\n\n{text}"
    assert "left this board" not in text, (
        "#900 is open and still matches; the fetch simply did not reach it. "
        f"The board cannot call that a departure.\n\n{text}")
    assert "#900" in text, (
        f"#900 fell off the page and nothing on the board says so.\n\n{text}")
    assert "page" in text, (
        f"the page limit is the cause and is never named.\n\n{text}")


def test_gl_mrs_declines_departures_on_a_capped_page(monkeypatch):
    mrs = [_mr(1, "running"), _mr(2, "running")]
    previous = {"mrs": {str(m["iid"]): gl_tier._snap_entry(m) for m in mrs}}
    lines = gl_tier.render([mrs[0]], {"1"}, [], {}, [], [], previous,
                           label="scope author=@me", page_capped=True)
    text = "\n".join(lines)

    assert "left this board" not in text, text
    assert "!2" in text, f"!2 vanished and nothing says so.\n\n{text}"
    assert "page" in text, text


def test_a_departure_makes_the_tier_unhealthy(state_dir, monkeypatch):
    """`healthy` has one consumer — `quiet_when_healthy`, which drops the
    tier's whole output. A departure-only tick is entirely elided rows plus the
    summary line, so a healthy verdict there suppresses the one arm added so a
    departure is not read as nothing happening. The board never reaches stdout.
    """
    # #2024: default_branch_report now also reports the standing poller's
    # own health as a third, independent value -- see its own docstring.
    monkeypatch.setattr(tier, "default_branch_report",
                        lambda *a, **k: ([], True, True))
    monkeypatch.setattr(tier, "repo_name", lambda: "o/r")
    monkeypatch.setattr(tier, "watch_coverage", lambda: {"1005", "1013"})
    monkeypatch.setattr(tier.subprocess, "run",
                        lambda cmd, *a, **k: _Result(json.dumps(
                            [_pr(1005, [GREEN_LEG]), _pr(1013, [GREEN_LEG])])))
    _, first = tier.radar_report({"_arg": "", "_watch": lambda *a, **k: "alive"})
    assert first is True, "fixture drifted — the cold board must be healthy"

    monkeypatch.setattr(tier, "watch_coverage", lambda: {"1005"})
    monkeypatch.setattr(tier.subprocess, "run",
                        lambda cmd, *a, **k: _Result(json.dumps(
                            [_pr(1005, [GREEN_LEG])])))
    _, healthy = tier.radar_report({"_arg": "", "_watch": lambda *a, **k: "alive"})

    assert healthy is False, (
        "#1013 left the board and the tier still reports healthy, so "
        "quiet_when_healthy drops the departure notice entirely")


def test_departed_note_survives_a_non_decimal_id(monkeypatch):
    """`isdigit()` is True for superscripts and other non-decimal digits, where
    `int()` raises. Unreachable from a real iid, reachable from a snapshot file
    that was corrupted — and a board that crashes on reading its own state is
    a worse outcome than one that sorts an odd id lexically."""
    note = tier.snapshot.departed_note(["²", "12"], "PR", "#", "gh-pr:<n>")
    assert note and "12" in note[0]


def test_an_excluded_mr_is_not_a_departure(monkeypatch):
    """An exclusion removes a row, not a member. Counting it as departed would
    report the operator's own standing decision back to them as a merge."""
    mrs = [_mr(1, "failed"), _mr(2, "failed")]
    previous = {"mrs": {str(m["iid"]): gl_tier._snap_entry(m) for m in mrs}}
    lines = gl_tier.render(mrs, {"1", "2"}, [], {}, [], [], previous,
                           label="scope author=@me", excluded={"2"})
    text = "\n".join(lines)
    # Two pins, same reason as the pruning case in test_watch_radar.py: the
    # absence has an innocent explanation (no board) and a second one (the
    # exclusion never applied), and only the third is the claim.
    assert "1 excluded" in text, f"the exclusion never applied.\n\n{text}"
    assert "left this board" not in text, text
