"""A PR/MR wedged in `running` must come back to the delta board (#1025).

After the first tick a board is a delta: a row prints iff `cold or moved or
notable or _is_standing_problem`. `running` is deliberately not a standing
problem — a pipeline in progress is the ordinary state of a PR that was just
pushed. But it is also the only state that can persist indefinitely *while
being wrong*: a wedged leg, a runner that never picks the job up, a workflow
waiting on an approval nobody will give. None of those ever changes, so the
snapshot never mismatches and the row is suppressed on every subsequent tick.

The signal is time since the entry's own reported facts last changed, carried
on the snapshot as `_since` and excluded from the delta comparison — a
timestamp inside the compared facts would make every row differ every tick and
collapse the delta entirely, which is the trap this file also pins.
"""
from __future__ import annotations

import importlib.util
import json
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


snapshot = _module("watch_snapshot_1025", WATCH_DIR / "tiers" / "_snapshot.py")
gh = _module("watch_gh_prs_1025", WATCH_DIR / "tiers" / "gh_prs.py")
gl = _module("watch_gl_mrs_1025", WATCH_DIR / "tiers" / "gl_mrs.py")

T0 = "2026-08-08T00:00:00Z"
T_30M = "2026-08-08T00:30:00Z"
T_5H = "2026-08-08T05:00:00Z"


# ---------------------------------------------------------------------------
# 1. the shared bookkeeping — `_snapshot`
# ---------------------------------------------------------------------------

def test_since_is_not_part_of_the_compared_facts():
    entry = {"checks": "running"}
    stamped = snapshot.stamp(entry, None, now=T0)
    assert snapshot.facts(stamped) == entry, (
        "a timestamp inside the compared facts makes every row moved every "
        "tick, which is the delta collapsing rather than a staleness signal")


def test_since_is_carried_forward_while_the_facts_hold_still():
    first = snapshot.stamp({"checks": "running"}, None, now=T0)
    second = snapshot.stamp({"checks": "running"}, first, now=T_5H)
    assert second[snapshot.SINCE_KEY] == T0


def test_since_is_restamped_when_a_fact_changes():
    first = snapshot.stamp({"checks": "running"}, None, now=T0)
    second = snapshot.stamp({"checks": "success"}, first, now=T_5H)
    assert second[snapshot.SINCE_KEY] == T_5H


def test_unchanged_minutes_is_none_when_it_cannot_be_told():
    """Three states. An entry with no `_since` — a snapshot written before this
    landed, or a corrupted one — is *unknown*, never zero."""
    assert snapshot.unchanged_minutes({"checks": "running"}, now=T_5H) is None
    assert snapshot.unchanged_minutes({snapshot.SINCE_KEY: "not a date"},
                                      now=T_5H) is None
    assert snapshot.unchanged_minutes(None, now=T_5H) is None
    assert snapshot.unchanged_minutes({snapshot.SINCE_KEY: T0},
                                      now=T_5H) == pytest.approx(300.0)


# ---------------------------------------------------------------------------
# 2. gh-prs — the board the issue was filed against
# ---------------------------------------------------------------------------

def _gh_pr(number=1, checks="running"):
    return {"number": number, "_checks": checks, "headRefOid": "a" * 40,
            "isDraft": False, "mergeable": "MERGEABLE", "reviewDecision": "",
            "title": f"pr {number}", "url": "u", "headRefName": "b",
            "baseRefName": "master", "labels": [], "author": {"login": "me"},
            "updatedAt": T0, "createdAt": T0, "additions": 1, "deletions": 1,
            "changedFiles": 1, "assignees": []}


def _gh_previous(pr, since):
    entry = dict(gh.snap_entry(pr))
    if since is not None:
        entry[snapshot.SINCE_KEY] = since
    return {"prs": {str(pr["number"]): entry}}


def test_gh_a_running_pr_that_just_moved_is_still_elided():
    """The elision is kept — this fix must not reprint every running PR."""
    pr = _gh_pr()
    lines = gh.render([pr], set(), [], [], _gh_previous(pr, T_30M), "L",
                      now=T_5H, stale_running_minutes=0)
    assert "1 unchanged not shown" in "\n".join(lines)
    assert "[running" not in "\n".join(lines)


def test_gh_a_running_pr_unchanged_past_the_threshold_is_shown_again():
    pr = _gh_pr()
    lines = gh.render([pr], set(), [], [], _gh_previous(pr, T0), "L",
                      now=T_5H, stale_running_minutes=240)
    text = "\n".join(lines)
    assert "#1" in text
    assert "unchanged" in text, "the row must say why it came back"


def test_gh_a_running_pr_inside_the_threshold_stays_elided():
    pr = _gh_pr()
    lines = gh.render([pr], set(), [], [], _gh_previous(pr, T_30M), "L",
                      now=T_5H, stale_running_minutes=600)
    assert "1 unchanged not shown" in "\n".join(lines)
    assert "[running" not in "\n".join(lines)


def test_gh_an_entry_with_no_since_is_not_reported_as_fresh_or_stale():
    """Unknown is not zero and it is not stale either. The next write stamps
    it, so the window is one radar interval plus one threshold, once."""
    pr = _gh_pr()
    lines = gh.render([pr], set(), [], [], _gh_previous(pr, None), "L",
                      now=T_5H, stale_running_minutes=1)
    assert "1 unchanged not shown" in "\n".join(lines)
    assert "[running" not in "\n".join(lines)


def test_gh_a_green_pr_is_never_stale():
    pr = _gh_pr(checks="success")
    lines = gh.render([pr], set(), [], [], _gh_previous(pr, T0), "L",
                      now=T_5H, stale_running_minutes=1)
    assert "1 unchanged not shown" in "\n".join(lines)
    assert "[running" not in "\n".join(lines)


def test_gh_snapshot_written_by_radar_report_carries_since(tmp_path, monkeypatch):
    monkeypatch.setattr(gh.transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(gh.snapshot.transport, "STATE_DIR", str(tmp_path))
    # #2024: default_branch_report now also reports the standing poller's
    # own health as a third, independent value -- see its own docstring.
    monkeypatch.setattr(gh, "default_branch_report",
                        lambda *a, **k: ([], True, True))
    monkeypatch.setattr(gh, "_reconcile_one", lambda p: ("", []))
    monkeypatch.setattr(gh, "live_open_prs", lambda f: [_gh_pr()])
    monkeypatch.setattr(gh, "repo_name", lambda: "o/r")
    monkeypatch.setattr(gh, "watch_coverage", lambda: {"1"})

    gh.radar_report({"_arg": "", "_watch": lambda *a, **k: "alive"})

    written = list(tmp_path.glob("*snapshot.json"))
    assert written, "no snapshot written"
    entry = json.loads(written[0].read_text(encoding="utf-8"))["prs"]["1"]
    assert entry.get(snapshot.SINCE_KEY), "the snapshot cannot answer 'since when'"


# ---------------------------------------------------------------------------
# 3. gl-mrs — the same predicate, the same omission
# ---------------------------------------------------------------------------

def _gl_mr(iid=7, pipeline="running"):
    return {"iid": iid, "_pipeline": pipeline, "_pipeline_id": "99",
            "draft": False, "has_conflicts": False, "title": f"mr {iid}",
            "web_url": "u", "source_branch": "b", "target_branch": "master",
            "labels": [], "author": {"username": "me"}, "updated_at": T0,
            "created_at": T0, "detailed_merge_status": "mergeable"}


def _gl_previous(mr, since):
    entry = dict(gl._snap_entry(mr))
    if since is not None:
        entry[snapshot.SINCE_KEY] = since
    return {"mrs": {str(mr["iid"]): entry}}


def test_gl_a_running_mr_unchanged_past_the_threshold_is_shown_again():
    mr = _gl_mr()
    lines = gl.render([mr], set(), [], {}, [], [], _gl_previous(mr, T0),
                      now=T_5H, stale_running_minutes=240)
    text = "\n".join(lines)
    assert "!7" in text
    assert "unchanged" in text


def test_gl_a_running_mr_inside_the_threshold_stays_elided():
    mr = _gl_mr()
    lines = gl.render([mr], set(), [], {}, [], [], _gl_previous(mr, T_30M),
                      now=T_5H, stale_running_minutes=600)
    assert "1 unchanged not shown" in "\n".join(lines)
    assert "[running" not in "\n".join(lines)


def test_gl_a_pending_mr_counts_as_in_progress():
    """A runner that never picks the job up renders `pending`, not `running`."""
    mr = _gl_mr(pipeline="pending")
    lines = gl.render([mr], set(), [], {}, [], [], _gl_previous(mr, T0),
                      now=T_5H, stale_running_minutes=240)
    assert "unchanged" in "\n".join(lines)


def test_gl_a_wedged_pending_mr_is_not_labelled_running():
    """The mark names the state observed, never a fixed literal.

    A pipeline stuck at `pending` never started. Printing it as `running` is the
    board telling a maintainer something other than what it saw — on the one row
    that exists because nothing else was going to mention it.
    """
    mr = _gl_mr(pipeline="pending")
    text = "\n".join(gl.render([mr], set(), [], {}, [], [], _gl_previous(mr, T0),
                               now=T_5H, stale_running_minutes=240))
    assert "[pending 5h unchanged]" in text
    assert "running" not in text


def test_gh_a_wedged_running_pr_is_labelled_running():
    mr = _gh_pr()
    text = "\n".join(gh.render([mr], set(), [], [], _gh_previous(mr, T0), "L",
                               now=T_5H, stale_running_minutes=240))
    assert "[running 5h unchanged]" in text


def test_the_label_is_shared_by_both_tiers():
    """One copy, in `_snapshot`. A second copy is how a fixed defect comes back
    — this label carried exactly such a defect while it was duplicated."""
    assert not hasattr(gh, "stale_running_label")
    assert not hasattr(gl, "stale_running_label")
    assert snapshot.unchanged_label(300.0, "pending") == "pending 5h unchanged"
    assert snapshot.unchanged_label(45.0, "running") == "running 45m unchanged"
