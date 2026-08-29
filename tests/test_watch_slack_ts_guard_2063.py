"""A non-numeric `ts` must not raise out of the slack poller -- #2063.

`_fetch`'s own docstring promises `(None, why)` on any failure, and every
other arm honours it -- an unreachable Slack, a revoked token, an unexpected
payload shape. The `float(ts)` conversions inside the sort/filter keys were
the one path that did not: a non-numeric `ts` raised `ValueError` straight
out of `poll()` and killed the watcher, instead of reporting and staying
alive like every neighbouring failure arm.

Reachability is not demonstrated in the issue -- Slack's own API does not
return a non-numeric `ts`, and HTTPS plus same-origin redirects stand in
front of a spoofed response. These tests guard the contract as stated,
same as the issue's own "what would settle it".

Mocked at the same seam as `tests/test_watch_slack_poller_2031.py`:
`_api.call` and `resolve_bot_user_id`, never a live Slack workspace.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"
sys.path.insert(0, str(WATCH_DIR))

SOURCE_DIR = WATCH_DIR / "sources" / "slack"
POLLER = SOURCE_DIR / "poller.py"

CHANNEL = "C0123456"
BOT_UID = "U_BOT"


@pytest.fixture(autouse=True)
def _fake_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same reasoning as `tests/test_watch_slack_poller_2031.py`'s own
    fixture of this name: every test here that calls `poll()` unmocked
    goes through the real `_auth.get_bot_token_or_none()`, which also
    reads `~/.config/slack/bot_token` -- a file a maintainer's own machine
    can have and CI never will. Without this, "did not raise" and "still
    delivers the event" pass or fail depending on whose machine happens to
    have a Slack token configured, not on whether the guard is correct."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake-not-a-real-token-2063")


def _load_poller():
    spec = importlib.util.spec_from_file_location("slack_watch_poller_2063", POLLER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _msg(ts, text: str = "hi") -> dict:
    return {"ts": ts, "text": text}


# --- _fetch: does not raise, reports instead --------------------------------

def test_fetch_with_a_non_numeric_message_ts_does_not_raise_and_reports() -> None:
    """The bug as filed: a non-numeric `ts` inside the sort/filter key must
    not raise `ValueError` out of `_fetch`. It must return `(None, why)`,
    the same shape every other failure arm already uses."""
    poller = _load_poller()

    def fake_call(method, token, *, params=None, body=None, timeout=15):
        return {"ok": True, "messages": [_msg("not-a-number")], "has_more": False}

    with mock.patch.object(poller._api, "call", fake_call):
        out, err = poller._fetch(CHANNEL, None, "xoxb-fake", cursor="1.0")

    assert out is None
    assert err.startswith("ERROR:")
    assert "not-a-number" in err


def test_fetch_with_a_non_numeric_cursor_does_not_raise_and_reports() -> None:
    """The stored cursor itself is converted too (`float(cursor)`) -- a
    corrupted state value must fail the same way, not raise."""
    poller = _load_poller()

    def fake_call(method, token, *, params=None, body=None, timeout=15):
        return {"ok": True, "messages": [_msg("2.0")], "has_more": False}

    with mock.patch.object(poller._api, "call", fake_call):
        out, err = poller._fetch(CHANNEL, None, "xoxb-fake", cursor="also-not-a-number")

    assert out is None
    assert err.startswith("ERROR:")


def test_fetch_with_a_numeric_ts_still_goes_all_the_way_through() -> None:
    """Positive control for the two tests above: a well-formed `ts` must
    still produce a result -- without this, "does not raise" would pass
    even if `_fetch` never actually processed anything."""
    poller = _load_poller()

    def fake_call(method, token, *, params=None, body=None, timeout=15):
        return {"ok": True, "messages": [_msg("10.000000")], "has_more": False}

    with mock.patch.object(poller._api, "call", fake_call):
        out, err = poller._fetch(CHANNEL, None, "xoxb-fake", cursor="9.500000")

    assert err == ""
    assert out is not None
    assert [m["ts"] for m in out] == ["10.000000"]


# --- _anchor: same contract on cold start -----------------------------------

def test_anchor_with_a_non_numeric_ts_does_not_raise_and_reports() -> None:
    """`_anchor`'s own docstring claims the same `(None, why)` shape as
    `_fetch` on any failure -- its `max(..., key=lambda m: float(m["ts"]))`
    had the identical unguarded conversion."""
    poller = _load_poller()

    def fake_call(method, token, *, params=None, body=None, timeout=15):
        return {"ok": True, "messages": [_msg("garbage-ts")]}

    with mock.patch.object(poller._api, "call", fake_call):
        ts, err = poller._anchor(CHANNEL, None, "xoxb-fake")

    assert ts is None
    assert err.startswith("ERROR:")


def test_anchor_with_a_numeric_ts_still_resolves() -> None:
    """Positive control: a well-formed anchor `ts` must still resolve."""
    poller = _load_poller()

    def fake_call(method, token, *, params=None, body=None, timeout=15):
        return {"ok": True, "messages": [_msg("5000000000.000000")]}

    with mock.patch.object(poller._api, "call", fake_call):
        ts, err = poller._anchor(CHANNEL, None, "xoxb-fake")

    assert err == ""
    assert ts == "5000000000.000000"


# --- end to end through poll(): alive and reporting, not dead --------------

def test_poll_with_a_non_numeric_ts_stays_alive_and_reports_rather_than_raising() -> None:
    """The issue's own settling condition: a non-numeric `ts` must leave the
    poller alive and reporting (a `slack_unreachable` event, cursor left
    untouched so the next tick retries) rather than raising out of `poll`."""
    poller = _load_poller()
    established = {"cursor": "0.5", "bot_user_id": BOT_UID}
    with mock.patch.object(poller, "resolve_bot_user_id", return_value=BOT_UID), \
         mock.patch.object(poller, "_fetch", return_value=(None, "ERROR: non-numeric ts 'x' from Slack")):
        events, new_state = poller.poll(established, {"source": "slack", "id": CHANNEL, "only": []})

    assert [e["event"] for e in events] == ["slack_unreachable"]
    assert new_state["cursor"] == "0.5"


def test_poll_with_a_numeric_ts_still_delivers_the_event() -> None:
    """Positive control for the test above: a healthy `ts` must still
    deliver its event through the real (unmocked) `_fetch`, end to end."""
    poller = _load_poller()

    def fake_call(method, token, *, params=None, body=None, timeout=15):
        return {"ok": True, "messages": [_msg("2.0", "hello")], "has_more": False}

    with mock.patch.object(poller._api, "call", fake_call), \
         mock.patch.object(poller, "resolve_bot_user_id", return_value=BOT_UID):
        events, new_state = poller.poll(
            {"cursor": "1.0", "bot_user_id": BOT_UID}, {"source": "slack", "id": CHANNEL, "only": []})

    assert [e["event"] for e in events] == ["slack_message"]
    assert new_state["cursor"] == "2.0"
