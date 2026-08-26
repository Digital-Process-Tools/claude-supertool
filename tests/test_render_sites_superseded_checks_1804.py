"""#1804 — three more renders still count a superseded check run as red.

#1792 fixed the merge gate (`gh-pr:N:status`, `gh-pr:N`, both paths of
`gh-pr-merge`): a check run a later run of the same name replaced is no
longer counted as a live failure there. Three other renders called
`_checks.github_states()` / `_checks.summarize()` directly and were left
with the old arithmetic:

* `presets/git/status.py` — the `Checks:` line on the branch's PR/MR section
* `presets/dashboard/dashboard.py` — `_build_pr()`, which feeds `pr_verdict()`
  and `_red_ref()` for the board's per-PR row
* `presets/github/pr_create.py` — `checks_section()`, the post-create summary

Each gets the same pair of fixtures: a leg superseded by a later run of the
same name (must render live/green, not red) and a leg that never existed at
all (a smaller rollup, so the render cannot pass by accident — a fixture with
zero legs and a fixture with one live leg render differently and both have to
say so correctly).
"""
from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "presets"))
import _checks  # noqa: E402

GH_ZERO = "0001-01-01T00:00:00Z"
DETAILS = "https://github.com/o/r/actions/runs/{run}/job/{job}"


def _leg(name: str, conclusion: str, started: str, completed: str,
         job: str = "") -> dict:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": "COMPLETED" if conclusion else "IN_PROGRESS",
        "conclusion": conclusion,
        "startedAt": started,
        "completedAt": completed or GH_ZERO,
        "detailsUrl": DETAILS.format(run="1", job=job) if job else "",
    }


def _superseded_rollup() -> list:
    """One `build` leg that failed, then a later `build` leg that passed.

    The stale failure must not decide the tally; #1792's discriminator is
    timing (started strictly after the other completed), not name.
    """
    return [
        _leg("build", "FAILURE", "2026-08-17T22:23:27Z",
             "2026-08-17T22:23:58Z", job="1"),
        _leg("build", "SUCCESS", "2026-08-18T06:33:20Z",
             "2026-08-18T06:33:41Z", job="2"),
    ]


def _no_run_rollup() -> list:
    """A leg that never existed at all — the fixture a superseded fixture
    must not be confused with. Empty, not merely 'all green'."""
    return []


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# git-status
# ===========================================================================

status = _load("presets/git/status.py", "git_status_1804")


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git"], 0, stdout, "")


def _dead(rc: int = 1) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git"], rc, "", "")


def _status_out(monkeypatch, rollup: list) -> str:
    def fake(args, timeout=None):
        head = args[0] if args else ""
        if head == "for-each-ref":
            return _ok("")
        if head == "log":
            return _ok("abc1234 2026-08-22 me | subject" + chr(10))
        if head in ("stash", "status"):
            return _ok("")
        if head == "branch":
            return _ok("* feature abc1234 [origin/feature] subject" + chr(10))
        if head == "rev-parse":
            if "--abbrev-ref" in args:
                return _ok("feature" + chr(10))
            return _ok("7" * 40 + chr(10))
        if head == "rev-list":
            return _ok("0" + chr(9) + "0" + chr(10))
        if head == "diff" and "--shortstat" in args:
            return _dead(128)
        return _ok("")

    monkeypatch.setattr(status, "_spawn_git", fake)

    def hosted(cmd):
        if cmd[:2] == ["glab", "mr"]:
            return None
        if cmd[:2] == ["gh", "pr"]:
            return {
                "number": 1804, "title": "t", "state": "OPEN",
                "baseRefName": "master", "statusCheckRollup": rollup,
                "body": "", "additions": 1, "deletions": 0, "changedFiles": 1,
                "headRefOid": "7" * 40, "mergeable": "MERGEABLE",
            }
        return None

    monkeypatch.setattr(status, "_hosted_request", hosted)
    monkeypatch.setattr(sys, "argv", ["status.py"])

    buf = io.StringIO()
    with redirect_stdout(buf):
        status.main()
    return buf.getvalue()


def _checks_line(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("State:") and "Checks:" in line:
            return line
    raise AssertionError("no Checks: line in:\n" + out)


def test_git_status_superseded_leg_does_not_read_red(monkeypatch):
    line = _checks_line(_status_out(monkeypatch, _superseded_rollup()))
    assert "superseded" in line, line
    assert _checks.NOT_GREEN not in line, (
        f"the stale failure was superseded by a later run of the same name "
        f"and still reads NOT ALL GREEN: {line!r}")


def test_git_status_no_run_at_all_is_not_confused_with_superseded(monkeypatch):
    """The must-fire control: an empty rollup is not a green tally either."""
    out = _status_out(monkeypatch, _no_run_rollup())
    line = _checks_line(out)
    assert "superseded" not in line, (
        f"nothing was superseded — this PR simply has no check runs: {line!r}")


# ===========================================================================
# dashboard
# ===========================================================================

sys.path.insert(0, str(ROOT / "tests"))
from _preset_loader import load_preset_module  # noqa: E402

dashboard = load_preset_module("dashboard", "dashboard", prefix="dashboard_1804_")


def _payload(rollup: list) -> dict:
    return {
        "number": 1804, "headRefName": "fix/1804", "title": "t",
        "url": "https://github.com/o/r/pull/1804", "headRefOid": "7" * 40,
        "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
        "isDraft": False, "statusCheckRollup": rollup, "body": "", "labels": [],
    }


def test_dashboard_build_pr_drops_the_superseded_leg_from_states(monkeypatch):
    monkeypatch.setattr(dashboard._gh_pr, "_reconcile_checks",
                        lambda d: ("", []))
    pr = dashboard._build_pr(_payload(_superseded_rollup()), {}, "")
    assert pr.states == ["SUCCESS"], (
        f"the superseded FAILURE leg is still in the states the verdict is "
        f"computed from: {pr.states!r}")
    word, why = dashboard.pr_verdict(pr)
    assert word == dashboard.MERGE, (
        f"GitHub calls this PR clean and the board still refuses to say "
        f"MERGE: {word} — {why}")


def test_dashboard_build_pr_empty_rollup_is_unestablished_not_merge(monkeypatch):
    """Must-fire control: no runs at all is UNKNOWN, never MERGE."""
    monkeypatch.setattr(dashboard._gh_pr, "_reconcile_checks",
                        lambda d: ("", []))
    pr = dashboard._build_pr(_payload(_no_run_rollup()), {}, "")
    assert pr.states == [], pr.states
    word, why = dashboard.pr_verdict(pr)
    assert word == dashboard.UNKNOWN, (why)


def test_dashboard_red_ref_does_not_point_at_a_superseded_failure(monkeypatch):
    """`_red_ref` names the leg `next:` sends the reader to read. A stale,
    already-superseded failure must not be offered as the thing to read."""
    monkeypatch.setattr(dashboard._gh_pr, "_reconcile_checks",
                        lambda d: ("", []))
    pr = dashboard._build_pr(_payload(_superseded_rollup()), {}, "")
    assert pr.red_ref is None, (
        f"the only failure on this rollup was superseded and _red_ref still "
        f"names it: {pr.red_ref!r}")


# ===========================================================================
# gh-pr-create's post-create summary
# ===========================================================================

pr_create = _load("presets/github/pr_create.py", "github_pr_create_1804")


def test_pr_create_checks_section_does_not_render_the_superseded_leg_red():
    lines, state = pr_create.checks_section(_superseded_rollup(), 30, "7" * 40)
    text = "\n".join(lines)
    assert "superseded" in text, text
    assert _checks.NOT_GREEN not in text, (
        f"the stale failure was superseded and the post-create summary "
        f"still reads NOT ALL GREEN: {text!r}")


def test_pr_create_checks_section_zero_runs_is_not_confused_with_superseded():
    lines, state = pr_create.checks_section(_no_run_rollup(), 30, "7" * 40)
    text = "\n".join(lines)
    assert "superseded" not in text, text
    assert state == pr_create.NO_CHECKS_YET, state
