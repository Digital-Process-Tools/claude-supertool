"""claude-oss#1499 -- `gh-prs` tier option `pr_exclude_events`, an event blacklist.

`heal()` used to fork every per-PR `github-pr` poller with `only=[]`, i.e. no
filter: every one of the source's ten events, on every PR, forever. A board
that does not want `comment_added` on a hundred PRs had no way to say so
short of unwatching each poller by hand.

The option is a blacklist over `sources/github-pr/events.json`: the poller's
`only=` is every declared key minus the excluded ones. Absent or empty keeps
today's `[]`. An unknown key is refused loudly -- `RadarError`, naming the
key and the vocabulary -- never dropped, because a typo silently ignored is a
filter the operator believes is on and is not (the house defect, one config
key over).

The option reaches the tier through `SUPERTOOL_RADAR_TIERS`, which
`radar.read_tiers` `json.loads` back into native types -- so a list arrives
as a list. A JSON-encoded *string* list is accepted as well, for a config
that stringified it by hand.
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


tier = _module("radar_gh_prs_oss1499", WATCH_DIR / "tiers" / "gh_prs.py")

# The vocabulary, read off the source's own declaration rather than retyped,
# so this test cannot pass on a stale copy while the tier reads a fresh one.
with open(WATCH_DIR / "sources" / "github-pr" / "events.json", encoding="utf-8") as _f:
    ALL_EVENTS = [e["key"] for e in json.load(_f)["events"]]

GREEN_LEG = {"name": "pytest", "status": "COMPLETED", "conclusion": "SUCCESS",
             "detailsUrl": "https://github.com/o/r/actions/runs/1/job/9"}


def _pr(number: int) -> dict:
    return {
        "number": number, "title": f"pr {number}", "state": "OPEN",
        "author": {"login": "me"}, "headRefName": f"fix/{number}",
        "baseRefName": "master", "headRefOid": "a" * 40, "labels": [],
        "isDraft": False, "mergeable": "MERGEABLE", "reviewDecision": "",
        "statusCheckRollup": [GREEN_LEG], "additions": 1, "deletions": 1,
        "changedFiles": 1, "updatedAt": "2026-08-07T10:00:00Z",
        "createdAt": "2026-08-07T09:00:00Z", "assignees": [],
        "url": f"https://github.com/o/r/pull/{number}",
    }


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


def _run(monkeypatch, options: dict, rows=None):
    """`(lines, calls)` -- the board plus every `(source, scope, only)` the
    tier asked its `_watch` for. Nothing is watched beforehand, so every PR
    row is healed and its spawn argv is observable."""
    rows = [_pr(7)] if rows is None else rows
    monkeypatch.setattr(tier, "default_branch_report",
                        lambda *a, **k: ([], True, True))
    monkeypatch.setattr(tier, "repo_name",
                        lambda: "Digital-Process-Tools/claude-supertool")
    monkeypatch.setattr(tier, "watch_coverage", lambda: set())
    monkeypatch.setattr(tier.subprocess, "run",
                        lambda cmd, *a, **k: _Result(json.dumps(rows)))
    calls: list[tuple[str, str, list[str]]] = []

    def watch(source, scope, only=None):
        calls.append((source, scope, list(only or [])))
        return "spawned"

    lines, _ = tier.radar_report({**options, "_arg": "", "_watch": watch})
    return lines, calls


def _pr_only(calls, number: str) -> list[str]:
    matching = [only for source, scope, only in calls
                if source == tier.SOURCE and scope == number]
    assert matching, f"no per-PR spawn for #{number} in {calls}"
    return matching[0]


# ---------------------------------------------------------------------------
# the option itself
# ---------------------------------------------------------------------------

def test_option_is_declared_so_radar_does_not_warn_it_unknown():
    assert "pr_exclude_events" in tier.RADAR_OPTIONS


def test_excluded_events_are_absent_and_the_rest_present(state_dir, monkeypatch):
    excluded = ["comment_added", "checks_pending"]
    _, calls = _run(monkeypatch, {"pr_exclude_events": excluded})
    only = _pr_only(calls, "7")
    # Negative: the blacklisted keys never reach the poller's argv.
    for key in excluded:
        assert key not in only
    # Positive control: every other declared key does -- an empty `only`
    # would pass the negative half alone and mean "no filter", the opposite
    # of what was asked.
    remaining = [k for k in ALL_EVENTS if k not in excluded]
    assert sorted(only) == sorted(remaining)
    assert len(only) == len(ALL_EVENTS) - len(excluded)


def test_the_feed_poller_keeps_its_own_filter(state_dir, monkeypatch):
    # The blacklist is about per-PR pollers; the discovery feed is a
    # different source with a different vocabulary and must not be touched.
    _, calls = _run(monkeypatch, {"pr_exclude_events": ["comment_added"]})
    feed = [only for source, _s, only in calls if source == tier.FEED_SOURCE]
    assert feed == [list(tier.FEED_ONLY)]


@pytest.mark.parametrize("value", [None, [], "", "[]"])
def test_absent_or_empty_keeps_the_unfiltered_spawn(state_dir, monkeypatch, value):
    options = {} if value is None else {"pr_exclude_events": value}
    _, calls = _run(monkeypatch, options)
    assert _pr_only(calls, "7") == []


def test_a_json_encoded_string_list_is_read_like_a_list(state_dir, monkeypatch):
    # `read_tiers` hands the tier native types, but a config that stringified
    # the list -- the shape `SUPERTOOL_`-prefixed op-config keys take on the
    # subprocess env -- must mean the same thing.
    _, calls = _run(monkeypatch,
                    {"pr_exclude_events": json.dumps(["merged", "closed"])})
    only = _pr_only(calls, "7")
    assert "merged" not in only and "closed" not in only
    assert sorted(only) == sorted(k for k in ALL_EVENTS if k not in ("merged", "closed"))


def test_an_unknown_key_is_refused_loudly_and_nothing_is_healed(state_dir, monkeypatch):
    with pytest.raises(tier.RadarError) as exc:
        _run(monkeypatch, {"pr_exclude_events": ["comment_added", "coment_addd"]})
    msg = str(exc.value)
    assert "coment_addd" in msg
    assert "pr_exclude_events" in msg
    for key in ALL_EVENTS:
        assert key in msg, f"the refusal must name the valid vocabulary; missing {key}"
    # Positive control for the "loudly": the same config with the typo fixed
    # goes through and spawns.
    _, calls = _run(monkeypatch, {"pr_exclude_events": ["comment_added"]})
    assert "comment_added" not in _pr_only(calls, "7")


def test_a_value_that_is_neither_list_nor_json_list_is_refused(state_dir, monkeypatch):
    with pytest.raises(tier.RadarError) as exc:
        _run(monkeypatch, {"pr_exclude_events": {"comment_added": True}})
    assert "pr_exclude_events" in str(exc.value)


# ---------------------------------------------------------------------------
# the caveat on the board
# ---------------------------------------------------------------------------

def test_footer_states_the_already_alive_caveat_only_when_active(state_dir, monkeypatch):
    lines, _ = _run(monkeypatch, {"pr_exclude_events": ["comment_added"]})
    note = [ln for ln in lines if "comment_added" in ln and "unwatch:github-pr:" in ln]
    assert len(note) == 1, lines
    assert "already alive" in note[0] or "already live" in note[0]
    # Control: no option, no note -- the sentence is about a filter that is
    # on, and printing it on an unfiltered board would claim one.
    lines, _ = _run(monkeypatch, {})
    assert not [ln for ln in lines if "unwatch:github-pr:" in ln], lines
