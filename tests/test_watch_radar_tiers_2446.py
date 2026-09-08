"""radar:tiers=NAME[,NAME...] selects a subset of registered tiers (#2446).

Today every registered tier in `ops.radar.radar_tiers` renders on every run --
there is no way to say "this session only cares about the CI board" out of one
`.supertool.json` shared with a fleet board. `tiers=` reads the same registry
#528 already built and filters it before any tier is resolved or reported.

A name in `tiers=` that is not in the registry is refused and named -- the
same "three states, not two" rule this repo applies everywhere else: a filter
that matched nothing named X must say so, not render byte-identical to a board
where X simply had nothing to report (the `milestne=x` typo case gives the
same reasoning, `docs/presets/watch.md:169`).

`tiers=` is read as the *entire* arg when present, never mixed with a further
per-tier filter on the same comma line: `author=@me,state=opened` already
means "one comma-joined argument, one tier's own vocabulary", and a `tiers=`
value is itself comma-joined tier names, so the two cannot share one line
without deciding whose commas are whose. That decision is left to a later
issue (the seam the issue text calls out for `_arg` fanout); this one is
`tiers=A,B` alone, or nothing.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


radar = _module("watch_radar_tiers_2446", WATCH_DIR / "radar.py")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Redirect radar state into tmp_path; no live spawn, no live reap."""
    monkeypatch.setattr(radar.transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(radar.dispatcher, "_spawn_poller",
                        lambda source, watcher_id, only: os.getpid())
    monkeypatch.setattr(radar.dispatcher, "reap_duplicate_pollers", lambda: [])
    return {"dir": tmp_path, "monkeypatch": monkeypatch}


class _FakeTier:
    RADAR_OPTIONS = {"window", "quiet_when_healthy"}
    RADAR_QUIET_DEFAULT = False  # this suite's boards are the report itself

    def __init__(self, lines, ok=True):
        self._lines, self._ok = lines, ok
        self.seen_options = None

    def radar_report(self, options=None):
        self.seen_options = options
        return list(self._lines), self._ok

    def radar_state(self, options=None):
        self.seen_options = options
        return list(self._lines)


def _register_two(env, name_a="gl-mrs", name_b="gl-runners"):
    a, b = _FakeTier([f"{name_a} board"]), _FakeTier([f"{name_b} board"])
    env["monkeypatch"].setenv(radar.TIERS_ENV, json.dumps({name_a: {}, name_b: {}}))
    env["monkeypatch"].setattr(
        radar, "_tier_module", lambda n: {name_a: a, name_b: b}.get(n))
    return a, b


# ---------------------------------------------------------------------------
# the negative case (must render only the named tier) needs a positive
# control (an unfiltered call must still render every tier) beside it --
# a fixture that renders nothing on both arms would pass the negative for
# the wrong reason.
# ---------------------------------------------------------------------------

def test_tiers_arg_renders_only_the_named_tier(env) -> None:
    a, b = _register_two(env)
    lines, ok, failures = radar.tier_reports("tiers=gl-runners")
    assert "gl-runners board" in lines
    assert "gl-mrs board" not in lines
    assert a.seen_options is None  # gl-mrs was never resolved, let alone called
    assert b.seen_options is not None
    assert failures == []
    assert ok is True


def test_an_unfiltered_call_still_renders_every_tier(env) -> None:
    a, b = _register_two(env)
    lines, ok, failures = radar.tier_reports("")
    assert "gl-mrs board" in lines
    assert "gl-runners board" in lines
    assert a.seen_options is not None
    assert b.seen_options is not None
    assert failures == []


def test_tiers_arg_naming_more_than_one_tier_renders_both(env) -> None:
    a, b = _register_two(env, name_a="gl-mrs", name_b="server-diag")
    lines, ok, failures = radar.tier_reports("tiers=gl-mrs,server-diag")
    assert "gl-mrs board" in lines
    assert "server-diag board" in lines
    assert failures == []


def test_tiers_arg_naming_an_unregistered_tier_is_refused_not_dropped(env) -> None:
    """The `milestne=x` typo case, one op over: a `tiers=` name absent from the
    registry must be named as a finding, never silently render as if that tier
    simply had nothing to show."""
    a, b = _register_two(env)
    lines, ok, failures = radar.tier_reports("tiers=gl-runners,gl-typo")
    assert "gl-runners board" in lines
    assert "gl-mrs board" not in lines
    assert any("gl-typo" in line and "WARNING" in line for line in lines)


def test_tiers_arg_selecting_nothing_registered_is_a_finding_not_a_clean_board(env) -> None:
    _register_two(env)
    lines, ok, failures = radar.tier_reports("tiers=nope")
    assert not any(line.endswith("board") for line in lines)
    assert any("nope" in line and "WARNING" in line for line in lines)


def test_state_view_honours_the_same_tiers_selection(env) -> None:
    """`radar:--state:tiers=...` must describe the same run `radar:tiers=...`
    would make -- a view naming a different tier set than the one radar acts
    on is the defect `docs/presets/watch.md` already gives this reasoning for
    (`--state` is an argument, not a second op, precisely so the two cannot
    drift apart)."""
    a, b = _register_two(env)
    lines, failures = radar.tier_states("tiers=gl-runners")
    joined = "\n".join(lines)
    assert "gl-runners:" in joined
    assert "gl-mrs:" not in joined
    assert failures == []
