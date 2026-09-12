"""`_wait_interruptible` must also break early on a set `_RELOAD_FLAG` (#2514).

Before this fix it checked only `stop_flag["stop"]` on each 1-second step;
`_RELOAD_FLAG` was consulted only at the top of the outer poll loop. Since
#2509 capped a rate-limit back-off at `MAX_RETRY_AFTER_SECONDS` (one hour), a
poller sleeping through that back-off would not notice a reload signal until
the whole sleep elapsed.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"
sys.path.insert(0, str(WATCH_DIR))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dispatcher = _load("watch_dispatcher_2514", WATCH_DIR / "dispatcher.py")


@pytest.fixture(autouse=True)
def reset_reload_flag():
    dispatcher._RELOAD_FLAG["reload"] = False
    yield
    dispatcher._RELOAD_FLAG["reload"] = False


def test_a_reload_set_mid_backoff_returns_control_well_before_it_elapses(monkeypatch):
    """A poller parked in a near-max back-off (3600s) must regain control
    within a handful of seconds of a reload being signalled, not after the
    whole sleep."""
    calls = {"n": 0}

    def fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] == 3:
            dispatcher._RELOAD_FLAG["reload"] = True

    monkeypatch.setattr(dispatcher.time, "sleep", fake_sleep)

    dispatcher._wait_interruptible(dispatcher.MAX_RETRY_AFTER_SECONDS,
                                   {"stop": False})

    got = calls["n"]
    assert got == 3, f"expected the sleep to be interrupted 3 seconds in, got {got} steps"


def test_an_ordinary_sleep_with_no_reload_signal_runs_its_full_duration(monkeypatch):
    """The must-not-fire twin: nothing sets the reload flag or the stop flag,
    so the full duration must still be slept -- a fix that always returns
    early would pass the case above for the wrong reason."""
    calls = {"n": 0}

    def fake_sleep(_seconds):
        calls["n"] += 1

    monkeypatch.setattr(dispatcher.time, "sleep", fake_sleep)

    dispatcher._wait_interruptible(5, {"stop": False})

    assert calls["n"] == 5
    assert dispatcher._RELOAD_FLAG["reload"] is False


def test_the_change_is_findable():
    from _changelog_findable import assert_change_is_findable
    assert_change_is_findable(2514)
