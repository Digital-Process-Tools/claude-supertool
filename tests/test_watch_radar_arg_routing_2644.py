"""Routing `_arg` to the tier it names, not to every registered tier (#2644).

`tier_reports` and `tier_states` handed the *same* `_arg` string to every
tier configured in `ops.radar.radar_tiers`. With one tier registered
nothing was observable -- there was only ever one candidate. With two or
more, an argument meant for one tier (`radar:gh-issue:2369`) also reached
every other tier's own `_arg`, which either ignored it or misread it, and
nothing in the output said which.

This suite is the fanout's own routing: a two-tier registration where the
argument names one of them (positive: only that tier gets it), a two-tier
registration where the argument does not name any registered tier by prefix
(the argument was never a tier selector at all -- an ordinary filter string
for a population tier's own vocabulary, e.g. `author=@me,milestne=x` --
so it must reach every tier unchanged, the pre-existing fanout, not be
zeroed out as an error: CI caught this the first version of this function
got backwards, breaking the pinned `#961` typo-detection contract in
`tests/test_radar_gl_mrs_unknown_token_961.py`), and the single-tier case
(positive control for backward compatibility: a bare argument with no
tier-name prefix still reaches the one tier registered, unchanged from
before this routing existed).
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


radar = _module("watch_radar_arg_routing_2644", WATCH_DIR / "radar.py")


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


def _register_two(env, name_a="gh-issue", name_b="gl-issue"):
    a, b = _FakeTier([f"{name_a} board"]), _FakeTier([f"{name_b} board"])
    env["monkeypatch"].setenv(radar.TIERS_ENV, json.dumps({name_a: {}, name_b: {}}))
    env["monkeypatch"].setattr(
        radar, "_tier_module", lambda n: {name_a: a, name_b: b}.get(n))
    return a, b


def _register_one(env, name="gh-issue"):
    a = _FakeTier([f"{name} board"])
    env["monkeypatch"].setenv(radar.TIERS_ENV, json.dumps({name: {}}))
    env["monkeypatch"].setattr(radar, "_tier_module", lambda n: {name: a}.get(n))
    return a


# ---------------------------------------------------------------------------
# the negative case (the other tier must never see the argument) needs the
# positive control (the named tier does see it) beside it -- a fixture that
# calls neither tier would pass the negative for the wrong reason.
# ---------------------------------------------------------------------------

def test_arg_prefixed_by_a_tier_name_reaches_only_that_tier(env) -> None:
    a, b = _register_two(env)
    lines, ok, failures = radar.tier_reports("gh-issue:2369")
    assert a.seen_options is not None
    assert a.seen_options["_arg"] == "gh-issue:2369"
    # gl-issue still renders its own board -- only `tiers=` removes a tier
    # from resolution -- but it must never see the arg meant for gh-issue.
    assert b.seen_options is not None
    assert b.seen_options["_arg"] == ""
    assert failures == []
    assert ok is True


def test_unfiltered_arg_still_reaches_every_tier(env) -> None:
    """Positive control for the empty-arg fanout: unchanged from before #2644."""
    a, b = _register_two(env)
    lines, ok, failures = radar.tier_reports("")
    assert a.seen_options is not None and a.seen_options["_arg"] == ""
    assert b.seen_options is not None and b.seen_options["_arg"] == ""
    assert failures == []


def test_arg_that_names_no_registered_tier_fans_out_unchanged(env) -> None:
    """An argument that is not a prefix of any registered tier's name was
    never a tier selector to begin with -- it is an ordinary filter string
    in a population tier's own vocabulary (`author=@me,milestne=x`), and
    every registered tier must still see it whole so it can judge it
    itself. The first version of this routing zeroed it out instead and
    broke the pinned `#961` typo-detection contract
    (`tests/test_radar_gl_mrs_unknown_token_961.py`) on real CI."""
    a, b = _register_two(env)
    lines, ok, failures = radar.tier_reports("author=@me,milestne=x")
    assert a.seen_options is not None and a.seen_options["_arg"] == "author=@me,milestne=x"
    assert b.seen_options is not None and b.seen_options["_arg"] == "author=@me,milestne=x"
    assert not any("matched no registered tier" in line for line in lines)


def test_single_registered_tier_gets_a_bare_arg_unchanged(env) -> None:
    """Backward compatibility: with only one tier registered there is no
    ambiguity to route around, so a bare number (no tier-name prefix) must
    still reach the one tier -- the shape `gh-issue`'s own docstring shows."""
    a = _register_one(env)
    lines, ok, failures = radar.tier_reports("2369")
    assert a.seen_options is not None
    assert a.seen_options["_arg"] == "2369"
    assert not any("matched no registered tier" in line for line in lines)


def test_tier_states_routes_the_same_way(env) -> None:
    a, b = _register_two(env)
    lines, failures = radar.tier_states("gh-issue:2369")
    assert a.seen_options is not None and a.seen_options["_arg"] == "gh-issue:2369"
    assert b.seen_options is not None and b.seen_options["_arg"] == ""


def test_a_registered_name_that_prefixes_another_registered_name_does_not_double_route(env) -> None:
    """Two registered tier names can themselves collide as prefixes of one
    another -- `"a"` and `"a:b"`, argument `"a:b"` -- and both would satisfy
    the plain prefix test. The longest (more specific) match must win rather
    than routing to both, which would be the exact fan-out #2644 removed."""
    a, b = _register_two(env, name_a="a", name_b="a:b")
    lines, ok, failures = radar.tier_reports("a:b")
    assert b.seen_options is not None
    assert b.seen_options["_arg"] == "a:b"
    assert a.seen_options is not None
    assert a.seen_options["_arg"] == ""
