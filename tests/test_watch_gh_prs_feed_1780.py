"""radar's GitHub PR tier gains a discovery feed (#1780, #1779).

`presets/watch/tiers/gh_prs.py` used to state, honestly, that it had no
discovery feed — a PR opened after a radar run was invisible until the next
tick, and the footer said `discovery: radar ticks only` on every board
regardless of whether that was still true. These cases drive the tier's own
wiring: the feed is spawned through the same `_watch` callable as a per-PR
poller, its status reaches the footer and the health verdict, and the #673
repo-target ambiguity that already gates per-PR healing gates the feed the
same way.

`tests/test_watch_github_pr_feed_1780.py` covers the source's own poll()
logic in isolation; this file covers the tier that spawns it.
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


tier = _module("watch_gh_prs_feed_1780", WATCH_DIR / "tiers" / "gh_prs.py")


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


def _fake_gh(monkeypatch, prs, code=0, err=""):
    def run(cmd, *a, **k):
        return _Result(json.dumps(prs), err, code)

    monkeypatch.setattr(tier.subprocess, "run", run)


def _no_default_branch(monkeypatch):
    monkeypatch.setattr(tier, "default_branch_report", lambda *a, **k: ([], True))


def _recording_watch(status_by_source: dict[str, str] | None = None):
    """A `_watch` that answers per source, and remembers every call it saw —
    the tier calls it once per PR number and once for the feed, and the tests
    below need to tell which call was which.
    """
    status_by_source = status_by_source or {}
    calls: list[tuple[str, str, list[str] | None]] = []

    def watch(source: str, scope: str, only: list[str] | None = None) -> str:
        calls.append((source, scope, list(only) if only else only))
        return status_by_source.get(source, "alive")

    return watch, calls


# ---------------------------------------------------------------------------
# the feed is spawned exactly like a per-PR poller — through `_watch`, once
# per report, over the tier's own resolved scope
# ---------------------------------------------------------------------------

def test_the_feed_is_asked_for_through_watch_not_spawned_directly(state_dir, monkeypatch):
    _fake_gh(monkeypatch, [_pr(1)])
    _no_default_branch(monkeypatch)
    watch, calls = _recording_watch()

    tier.radar_report({"_arg": "", "_watch": watch})

    feed_calls = [c for c in calls if c[0] == tier.FEED_SOURCE]
    assert len(feed_calls) == 1, calls
    assert feed_calls[0][1] == "@open", "the whole-repo default gets its alias"


def test_the_feed_scope_follows_the_resolved_filter(state_dir, monkeypatch):
    _fake_gh(monkeypatch, [_pr(1)])
    _no_default_branch(monkeypatch)
    watch, calls = _recording_watch()

    tier.radar_report({"_arg": "author=@me", "_watch": watch})

    feed_calls = [c for c in calls if c[0] == tier.FEED_SOURCE]
    assert feed_calls[0][1] == "author=@me"


def test_the_feed_is_spawned_with_every_declared_event(state_dir, monkeypatch):
    """Unlike `gl_mrs`'s hand-kept `DEFAULT_FEED_ONLY`, this tier's filter is
    every event the source declares — see `FEED_ONLY`'s own docstring for why
    a second hand-kept list was rejected."""
    _fake_gh(monkeypatch, [_pr(1)])
    _no_default_branch(monkeypatch)
    watch, calls = _recording_watch()

    tier.radar_report({"_arg": "", "_watch": watch})

    feed_calls = [c for c in calls if c[0] == tier.FEED_SOURCE]
    assert set(feed_calls[0][2]) == set(tier.FEED_ONLY)


# ---------------------------------------------------------------------------
# the footer names the feed's own state (#1780) — not the fixed sentence
# ---------------------------------------------------------------------------

def test_a_healthy_feed_reports_feed_ok(state_dir, monkeypatch):
    _fake_gh(monkeypatch, [_pr(1)])
    _no_default_branch(monkeypatch)
    watch, _ = _recording_watch()

    lines, healthy = tier.radar_report({"_arg": "", "_watch": watch})
    text = "\n".join(lines)

    assert "discovery: feed ok" in text
    assert "discovery: radar ticks only" not in text


def test_a_dead_feed_is_a_warning_and_costs_health(state_dir, monkeypatch):
    _fake_gh(monkeypatch, [_pr(1)])
    _no_default_branch(monkeypatch)
    watch, _ = _recording_watch({tier.FEED_SOURCE: "failed"})

    lines, healthy = tier.radar_report({"_arg": "", "_watch": watch})
    text = "\n".join(lines)

    assert "discovery: feed DOWN" in text
    assert "PR feed poller is down" in text
    assert not healthy


def test_a_capped_feed_is_a_distinct_warning(state_dir, monkeypatch):
    """Down-and-respawned and down-and-given-up are different facts and want
    different remedies -- conflating them is the #1602 shape one word over."""
    _fake_gh(monkeypatch, [_pr(1)])
    _no_default_branch(monkeypatch)
    watch, _ = _recording_watch({tier.FEED_SOURCE: "capped"})

    lines, healthy = tier.radar_report({"_arg": "", "_watch": watch})
    text = "\n".join(lines)

    assert "respawn capped" in text
    assert "no longer being respawned" in text
    assert not healthy


# ---------------------------------------------------------------------------
# #673 — the feed follows the same repo-target rule as per-PR healing
# ---------------------------------------------------------------------------

def test_under_a_repo_target_the_feed_is_never_asked_for(state_dir, monkeypatch):
    """A poller spawned here for this board's scope would be indistinguishable
    from the same scope started against another clone (#673) -- so nothing is
    asked for, exactly as heal() already declines to spawn per-PR watchers."""
    _fake_gh(monkeypatch, [_pr(1)])
    _no_default_branch(monkeypatch)
    monkeypatch.setattr(tier._repo_target, "target", lambda: "other/repo")
    watch, calls = _recording_watch()

    lines, healthy = tier.radar_report({"_arg": "", "_watch": watch})
    text = "\n".join(lines)

    assert not any(c[0] == tier.FEED_SOURCE for c in calls), calls
    assert "feed coverage UNKNOWN" in text
    assert "#673" in text
    assert not healthy


# ---------------------------------------------------------------------------
# blind and split-scope feeds must be visible, not just "alive"
# ---------------------------------------------------------------------------

def test_a_blind_feed_is_reported_even_though_it_is_alive(state_dir, monkeypatch):
    """A feed that reached GitHub and got a 401 raises nothing -- it returns
    cleanly, having seen nothing, and looks identical to a healthy one unless
    this is read separately (#1602's own argument, one tier over)."""
    _fake_gh(monkeypatch, [_pr(1)])
    _no_default_branch(monkeypatch)
    Path(tier.transport.state_path(tier.FEED_SOURCE, "@open")).write_text(
        json.dumps({"source_state": {"lookup": "unavailable",
                                     "error": "ERROR: gh auth login"}}),
        encoding="utf-8")
    watch, _ = _recording_watch()

    lines, healthy = tier.radar_report({"_arg": "", "_watch": watch})
    text = "\n".join(lines)

    assert "could not establish the population" in text
    assert "gh auth login" in text
    assert not healthy


def test_a_feed_live_on_another_scope_is_named_not_silently_ignored(state_dir, monkeypatch):
    _fake_gh(monkeypatch, [_pr(1)])
    _no_default_branch(monkeypatch)
    monkeypatch.setattr(tier, "other_feed_scopes", lambda scope: ["author=@x"])
    watch, _ = _recording_watch()

    lines, healthy = tier.radar_report({"_arg": "", "_watch": watch})
    text = "\n".join(lines)

    assert "author=@x" in text
    assert "is also live" in text or "NOTE" in text
    assert not healthy


# ---------------------------------------------------------------------------
# feed_scope — the canonical spelling that keeps one population to one poller
# ---------------------------------------------------------------------------

def test_feed_scope_of_the_default_filter_is_the_open_alias() -> None:
    assert tier.feed_scope({}) == "@open"
    assert tier.feed_scope() == "@open"


def test_feed_scope_of_a_named_filter_is_its_canonical_spelling() -> None:
    assert tier.feed_scope({"author": "@me", "state": "open"}) == "author=@me,state=open"


# ---------------------------------------------------------------------------
# radar_state — the feed row, never a call
# ---------------------------------------------------------------------------

def test_radar_state_reports_the_feed_pid_row_without_calling_anything(state_dir, monkeypatch):
    def boom(*a, **k):
        pytest.fail("radar_state reached the network")

    monkeypatch.setattr(tier.subprocess, "run", boom)

    lines = tier.radar_state({"_arg": ""})
    text = "\n".join(lines)

    assert "feed" in text
    assert "@open" in text


def test_radar_state_under_a_repo_target_says_feed_coverage_is_unknown(state_dir, monkeypatch):
    def boom(*a, **k):
        pytest.fail("radar_state reached the network")

    monkeypatch.setattr(tier.subprocess, "run", boom)
    monkeypatch.setattr(tier._repo_target, "target", lambda: "other/repo")

    lines = tier.radar_state({"_arg": ""})
    text = "\n".join(lines)

    assert "UNKNOWN" in text and "#673" in text


# ---------------------------------------------------------------------------
# _feed_warnings — the unit, independent of the report it feeds
# ---------------------------------------------------------------------------

def test_feed_warnings_empty_when_alive_and_alone() -> None:
    assert tier._feed_warnings("alive", "", []) == []


def test_feed_warnings_name_a_polling_error_distinctly_from_down() -> None:
    lines = tier._feed_warnings("alive", "ERROR: gh timed out", [])
    assert any("failing to poll" in l for l in lines)
    assert not any("is down" in l for l in lines)
