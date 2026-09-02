"""radar's GitHub PR tier gains a standing `gh-branch` poller (#2024).

The default-branch row in `presets/watch/tiers/gh_prs.py` answered "is master
green right now", on demand, once per radar tick -- and nothing pushed that
answer between ticks. `sources/gh-branch/poller.py` (#1953) already exists as
a source plugin, but nothing ever spawned or healed it: a hand-started
`watch:gh-branch:master` died with a reboot, a `pkill`, or a crash, and
nothing respawned it. This file covers the tier wiring that closes that,
mirroring `tests/test_watch_gh_prs_feed_1780.py`'s coverage of the sibling
`github-pr-feed` poller line for line where the shape is the same, and
diverging where it is not -- see `other_branch_scopes`'s own docstring for
the one place the hazard is worse here than for the feed.
"""
from __future__ import annotations

import importlib.util
import json
import os
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


tier = _module("watch_gh_prs_branch_poller_2024", WATCH_DIR / "tiers" / "gh_prs.py")


def _pr(number: int, **kw) -> dict:
    row = {
        "number": number, "title": f"pr {number}", "state": "OPEN",
        "author": {"login": "me"}, "headRefName": f"fix/{number}",
        "baseRefName": "master", "headRefOid": "a" * 40, "labels": [],
        "isDraft": False, "mergeable": "MERGEABLE", "reviewDecision": "",
        "statusCheckRollup": [{"name": "tests", "status": "COMPLETED",
                               "conclusion": "SUCCESS",
                               "detailsUrl": "https://github.com/o/r/actions/runs/1/job/9"}],
        "additions": 1, "deletions": 1, "changedFiles": 1,
        "updatedAt": "2026-08-07T10:00:00Z", "createdAt": "2026-08-07T09:00:00Z",
        "assignees": [], "url": f"https://github.com/o/r/pull/{number}",
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
def quiet_reconcile(monkeypatch):
    monkeypatch.setattr(tier, "_reconcile_one", lambda p: ("", []))


@pytest.fixture(autouse=True)
def hermetic_repo_env(monkeypatch):
    """See `test_watch_gh_prs_feed_1780.py`'s own fixture of this name for
    why this is needed independently of any cleanup elsewhere (#1979)."""
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)


def _fake_gh(monkeypatch, prs, code=0, err=""):
    def run(cmd, *a, **k):
        return _Result(json.dumps(prs), err, code)

    monkeypatch.setattr(tier.subprocess, "run", run)


def _no_board(monkeypatch):
    """Turn the default-branch board row off -- these cases are about the
    poller wiring, not the direct query `default_branch_report`'s own board
    content composes. Real callers thread `ref`/`repo`/`watch` straight
    through to `default_branch_report`, so this stub, unlike the feed's
    `_no_default_branch`, must still exercise `default_branch_report`'s own
    poller block -- it stubs `branch._head_commit` to fail cleanly instead of
    replacing the whole function, which is the one seam that keeps this file
    honest about testing the *real* spawn/report code path."""
    monkeypatch.setattr(tier.branch, "_head_commit",
                        lambda ref: ("", 0, "ERROR: no commits for this test"))


def _recording_watch(status_by_source: dict[str, str] | None = None):
    status_by_source = status_by_source or {}
    calls: list[tuple[str, str, list[str] | None]] = []

    def watch(source: str, scope: str, only: list[str] | None = None) -> str:
        calls.append((source, scope, list(only) if only else only))
        return status_by_source.get(source, "alive")

    return watch, calls


# ---------------------------------------------------------------------------
# the poller is asked for through `_watch`, over the resolved ref, with
# every declared event -- and never when the row is switched off
# ---------------------------------------------------------------------------

def test_the_branch_poller_is_asked_for_through_watch(state_dir, monkeypatch):
    _fake_gh(monkeypatch, [_pr(1)])
    _no_board(monkeypatch)
    watch, calls = _recording_watch()

    tier.radar_report({"_arg": "", "_watch": watch, "default_branch": "main"})

    branch_calls = [c for c in calls if c[0] == tier.BRANCH_SOURCE]
    assert len(branch_calls) == 1, calls
    assert branch_calls[0][1] == "main"


def test_the_branch_poller_subscribes_to_every_declared_event(state_dir, monkeypatch):
    """Not just `went_green` -- a `gh` outage that could not even look must
    arrive as `branch_unreachable`, or it arrives as silence, which is the
    same shape of bug this whole issue closes."""
    _fake_gh(monkeypatch, [_pr(1)])
    _no_board(monkeypatch)
    watch, calls = _recording_watch()

    tier.radar_report({"_arg": "", "_watch": watch, "default_branch": "main"})

    branch_calls = [c for c in calls if c[0] == tier.BRANCH_SOURCE]
    assert set(branch_calls[0][2]) == set(tier.BRANCH_ONLY)
    assert "branch_unreachable" in branch_calls[0][2]
    assert "went_green" in branch_calls[0][2]


def test_no_spawn_when_the_default_branch_is_switched_off(state_dir, monkeypatch):
    """`default_branch=""` is the operator's own call, honoured rather than
    overridden -- spawning a poller for a row they disabled would be exactly
    that override."""
    _fake_gh(monkeypatch, [_pr(1)])
    watch, calls = _recording_watch()

    lines, healthy = tier.radar_report(
        {"_arg": "", "_watch": watch, "default_branch": ""})

    assert not any(c[0] == tier.BRANCH_SOURCE for c in calls), calls
    assert not any("branch poller" in l for l in lines), lines


def test_a_bare_call_with_no_spawner_reports_the_poller_down(state_dir, monkeypatch):
    """`default_branch_report`'s own `watch` default is `_no_watch`, which
    always answers "failed" -- a caller with no spawner configured must not
    claim the standing poller is up."""
    lines, could_tell, poller_ok = tier.default_branch_report("main", "o/r")
    assert poller_ok is False
    assert any("poller is down" in l for l in lines), lines


# ---------------------------------------------------------------------------
# the standing poller is a third, independent claim -- not folded into
# `could_tell`, which is about the direct query this call just made
# ---------------------------------------------------------------------------

def test_a_dead_poller_does_not_change_could_tell(monkeypatch):
    """A red master reported by a perfectly healthy direct query is
    `could_tell=True` regardless of whether the standing poller is up.

    The board-content pipeline (`_head_commit` through `verdict`) is not
    this test's subject -- `verdict` is forced to `NOT_GREEN` directly, so a
    genuine finding is reported the same way `test_a_red_master_is_
    unaffected` in `test_radar_scope_seam_1077.py` drives it through real
    `gh` fakes; the two are complementary, not duplicates.
    """
    monkeypatch.setattr(tier.branch, "_head_commit",
                        lambda ref: ("a" * 40, 5, ""))
    monkeypatch.setattr(tier.branch, "_run_list", lambda ref: ([], ""))
    monkeypatch.setattr(tier.branch, "scope_for",
                        lambda repo, sha, selected, **kw: ("scope", [], False))
    monkeypatch.setattr(tier.branch, "_reconcile",
                        lambda repo, selected, fetched: (None, []))
    monkeypatch.setattr(tier.branch, "verdict",
                        lambda *a, **k: (tier.branch.NOT_GREEN, "forced red"))

    watch = lambda *a, **k: "failed"  # noqa: E731
    lines, could_tell, poller_ok = tier.default_branch_report("main", "o/r", watch)

    assert could_tell is True, lines
    assert poller_ok is False


def test_a_dead_poller_costs_health_even_when_the_board_is_healthy(
        state_dir, monkeypatch):
    """`radar_report`'s own `healthy` verdict must fold in `poller_ok`, not
    only `branch_ok` -- auditor finding on this same lane (class B): every
    `assert not healthy` case in this file up to this point routed through
    `_no_board`, which forces `branch._head_commit` to error and makes
    `branch_ok` False on its own, so none of them could tell whether
    `bool(branch_poller_ok)` actually participates in the conjunction or was
    silently dropped. Confirmed by deleting that conjunct locally: every
    other test in this file still passed. This one forces the board itself
    to be genuinely healthy (`could_tell=True`, via a forced `NOT_GREEN` --
    a red master the tier *could* tell, same technique as
    `test_a_dead_poller_does_not_change_could_tell` above) while the
    standing poller is down, and pins that `healthy` is False anyway.
    """
    _fake_gh(monkeypatch, [_pr(1)])
    monkeypatch.setattr(tier.branch, "_head_commit",
                        lambda ref: ("a" * 40, 5, ""))
    monkeypatch.setattr(tier.branch, "_run_list", lambda ref: ([], ""))
    monkeypatch.setattr(tier.branch, "scope_for",
                        lambda repo, sha, selected, **kw: ("scope", [], False))
    monkeypatch.setattr(tier.branch, "_reconcile",
                        lambda repo, selected, fetched: (None, []))
    monkeypatch.setattr(tier.branch, "verdict",
                        lambda *a, **k: (tier.branch.NOT_GREEN, "forced red"))
    watch, _ = _recording_watch({tier.BRANCH_SOURCE: "failed"})

    _direct_lines, could_tell, _poller_ok = tier.default_branch_report(
        "main", "o/r", watch)
    assert could_tell is True  # the board itself is healthy -- sanity check

    _healthy_lines, healthy = tier.radar_report(
        {"_arg": "", "_watch": watch, "default_branch": "main"})

    assert not healthy, (
        "the board's own query was healthy, but the standing poller is "
        "down -- radar_report's healthy verdict must still be False")


# ---------------------------------------------------------------------------
# blind, dead, capped and stray pollers must be visible, not just "alive"
# ---------------------------------------------------------------------------

def test_a_dead_branch_poller_is_a_warning_and_costs_health(state_dir, monkeypatch):
    _fake_gh(monkeypatch, [_pr(1)])
    _no_board(monkeypatch)
    watch, _ = _recording_watch({tier.BRANCH_SOURCE: "failed"})

    lines, healthy = tier.radar_report(
        {"_arg": "", "_watch": watch, "default_branch": "main"})
    text = "\n".join(lines)

    assert "default branch poller is down" in text
    assert not healthy


def test_a_capped_branch_poller_is_a_distinct_warning(state_dir, monkeypatch):
    _fake_gh(monkeypatch, [_pr(1)])
    _no_board(monkeypatch)
    watch, _ = _recording_watch({tier.BRANCH_SOURCE: "capped"})

    lines, healthy = tier.radar_report(
        {"_arg": "", "_watch": watch, "default_branch": "main"})
    text = "\n".join(lines)

    assert "no longer being respawned" in text
    assert not healthy


def test_a_blind_branch_poller_is_reported_even_though_it_is_alive(state_dir, monkeypatch):
    """A poller that reached GitHub and got a 401 raises nothing -- it
    returns cleanly, having established nothing, and looks identical to a
    healthy one unless this is read separately (#1602's own argument, one
    poller over)."""
    _fake_gh(monkeypatch, [_pr(1)])
    _no_board(monkeypatch)
    Path(tier.transport.state_path(tier.BRANCH_SOURCE, "main")).write_text(
        json.dumps({"source_state": {"lookup": "unavailable",
                                     "error": "ERROR: gh auth login"}}),
        encoding="utf-8")
    watch, _ = _recording_watch()

    lines, healthy = tier.radar_report(
        {"_arg": "", "_watch": watch, "default_branch": "main"})
    text = "\n".join(lines)

    assert "could not establish the branch's state" in text
    assert "gh auth login" in text
    assert not healthy


def test_a_poller_live_on_a_stray_ref_is_named_and_costs_health(state_dir, monkeypatch):
    """The kept-old-ref-after-a-rename hazard the issue names explicitly:
    after `master` -> `main`, a stale poller left on `master` keeps emitting
    `went_green` forever, indistinguishable from a real one. Reading and
    naming it here is the whole of this lane's answer -- see
    `other_branch_scopes`'s own docstring for why that is judged sufficient:
    it is loud (costs `healthy`, so `quiet_when_healthy` cannot hide it) and
    actionable (names the exact scope to `unwatch`)."""
    _fake_gh(monkeypatch, [_pr(1)])
    _no_board(monkeypatch)
    monkeypatch.setattr(tier, "other_branch_scopes", lambda scope: ["master"])
    watch, _ = _recording_watch()

    lines, healthy = tier.radar_report(
        {"_arg": "", "_watch": watch, "default_branch": "main"})
    text = "\n".join(lines)

    assert "'master'" in text
    assert "is also live" in text
    assert "unwatch" in text
    assert not healthy


# ---------------------------------------------------------------------------
# a healthy standing poller is silent, matching this section's existing
# "speak only when there is something to say" calibration (#1077)
# ---------------------------------------------------------------------------

def test_a_healthy_branch_poller_adds_no_line(state_dir, monkeypatch):
    _fake_gh(monkeypatch, [_pr(1)])
    _no_board(monkeypatch)
    watch, _ = _recording_watch()

    lines, _healthy = tier.radar_report(
        {"_arg": "", "_watch": watch, "default_branch": "main"})

    assert not any("branch poller" in l or "default branch poller" in l
                  for l in lines), lines


# ---------------------------------------------------------------------------
# other_branch_scopes -- the unit, read-only
# ---------------------------------------------------------------------------

def _write_live_pid(source: str, watcher_id: str) -> None:
    """A pid file `list_active_pids` will actually keep -- it prunes any
    entry whose process is not alive, so a made-up PID would silently vanish
    before `other_branch_scopes` ever saw it (this file's own process is the
    only PID a test can vouch for without spawning one)."""
    Path(tier.transport.pid_path(source, watcher_id)).write_text(
        f"{os.getpid()}\n", encoding="utf-8")


def test_other_branch_scopes_reads_pid_files_only(state_dir):
    _write_live_pid(tier.BRANCH_SOURCE, "master")

    others = tier.other_branch_scopes("main")

    assert others == ["master"]
    # read-only: nothing here may kill or spawn anything, so the pid file
    # this test wrote is still exactly where it put it.
    assert tier.transport.read_pid(tier.BRANCH_SOURCE, "master") == os.getpid()


def test_other_branch_scopes_excludes_this_boards_own_scope(state_dir):
    _write_live_pid(tier.BRANCH_SOURCE, "main")

    assert tier.other_branch_scopes("main") == []


# ---------------------------------------------------------------------------
# _branch_poller_warnings -- the unit, independent of the report it feeds
# ---------------------------------------------------------------------------

def test_branch_poller_warnings_empty_when_alive_and_alone() -> None:
    assert tier._branch_poller_warnings("alive", "", []) == []


def test_branch_poller_warnings_empty_when_freshly_respawned() -> None:
    assert tier._branch_poller_warnings("spawned", "", []) == []


def test_branch_poller_warnings_name_a_polling_error_distinctly_from_down() -> None:
    lines = tier._branch_poller_warnings("alive", "ERROR: gh timed out", [])
    assert any("failing to poll" in l for l in lines)
    assert not any("is down" in l for l in lines)


def test_branch_poller_warnings_name_unknown_coverage_with_the_issue_number() -> None:
    lines = tier._branch_poller_warnings("unknown", "", [])
    assert any("#673" in l for l in lines)
