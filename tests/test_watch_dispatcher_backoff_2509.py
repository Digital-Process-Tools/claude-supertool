"""Interval override and rate-limit back-off sleep math (#2509).

Both live in `presets/watch/dispatcher.py`'s poll loop: `interval_override()`
is the fleet-wide `SUPERTOOL_WATCH_INTERVAL` config key, and
`_retry_after_seconds()` turns a poller's own `retry_after` (written on a
rate-limit-shaped failure, see the source pollers' own tests) into how long
the loop should sleep instead of its ordinary `INTERVAL`.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from unittest import mock

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"
sys.path.insert(0, str(WATCH_DIR))

_d_spec = importlib.util.spec_from_file_location("watch_dispatcher_backoff", WATCH_DIR / "dispatcher.py")
assert _d_spec is not None and _d_spec.loader is not None
dispatcher = importlib.util.module_from_spec(_d_spec)
_d_spec.loader.exec_module(dispatcher)


# ---------------------------------------------------------------------------
# interval_override
# ---------------------------------------------------------------------------

def test_unset_env_means_use_the_sources_own_interval() -> None:
    with mock.patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop(dispatcher.SUPERTOOL_WATCH_INTERVAL_ENV, None)
        assert dispatcher.interval_override() is None


def test_a_positive_int_override_is_read() -> None:
    with mock.patch.dict("os.environ",
                         {dispatcher.SUPERTOOL_WATCH_INTERVAL_ENV: "5"}):
        assert dispatcher.interval_override() == 5


def test_a_non_numeric_override_is_ignored_not_raised() -> None:
    with mock.patch.dict("os.environ",
                         {dispatcher.SUPERTOOL_WATCH_INTERVAL_ENV: "soon"}):
        assert dispatcher.interval_override() is None


def test_a_zero_or_negative_override_is_ignored() -> None:
    """0 must not mean "poll every 0 seconds" -- treated the same as unset."""
    with mock.patch.dict("os.environ",
                         {dispatcher.SUPERTOOL_WATCH_INTERVAL_ENV: "0"}):
        assert dispatcher.interval_override() is None
    with mock.patch.dict("os.environ",
                         {dispatcher.SUPERTOOL_WATCH_INTERVAL_ENV: "-10"}):
        assert dispatcher.interval_override() is None


# ---------------------------------------------------------------------------
# _retry_after_seconds
# ---------------------------------------------------------------------------

def test_a_future_retry_after_yields_a_positive_sleep() -> None:
    future = time.gmtime(time.time() + 120)
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", future)
    seconds = dispatcher._retry_after_seconds(iso)
    assert seconds is not None
    assert 110 <= seconds <= 130


def test_a_past_retry_after_is_none_not_a_negative_sleep() -> None:
    past = time.gmtime(time.time() - 60)
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", past)
    assert dispatcher._retry_after_seconds(iso) is None


def test_absent_or_malformed_retry_after_is_none() -> None:
    """Must-not-fire twins: a normal poll (no retry_after at all) and a
    non-rate-limit failure's `new_state` must never produce a sleep override."""
    assert dispatcher._retry_after_seconds(None) is None
    assert dispatcher._retry_after_seconds("") is None
    assert dispatcher._retry_after_seconds("not-a-timestamp") is None
    assert dispatcher._retry_after_seconds(12345) is None


def test_an_implausibly_far_retry_after_is_capped() -> None:
    far = time.gmtime(time.time() + 100000)
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", far)
    seconds = dispatcher._retry_after_seconds(iso)
    assert seconds == dispatcher.MAX_RETRY_AFTER_SECONDS

