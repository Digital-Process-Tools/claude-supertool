"""Tests for presets/slack/publish.py -- issue #2032.

`_api.call` is mocked at the module seam, same style
`tests/test_devto.py` mocks `_OPEN` -- none of this touches a live Slack
workspace. Conftest sets `SUPERTOOL_NO_PUBLISH_CONFIRM=1` for the whole
suite, so `main()` below does not need `|force` to get past the
confirmation gate -- see `tests/test_security_publish_149.py` for the tests
that exercise that gate directly.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from _preset_loader import load_preset_module


def _load():
    return load_preset_module("slack", "publish", "sl_")


publish = _load()


# --- parse_args --------------------------------------------------------------

def test_parse_args_minimal() -> None:
    channel, text, thread_ts, force = publish.parse_args("C0123456|hello there")
    assert channel == "C0123456"
    assert text == "hello there"
    assert thread_ts is None
    assert force is False


def test_parse_args_with_thread_ts() -> None:
    channel, text, thread_ts, force = publish.parse_args(
        "C0123456|reply text|1699999999.000100")
    assert thread_ts == "1699999999.000100"


def test_parse_args_force_flag() -> None:
    _c, _t, _ts, force = publish.parse_args("C0123456|hi||force")
    assert force is True


def test_parse_args_no_force_by_default() -> None:
    _c, _t, _ts, force = publish.parse_args("C0123456|hi")
    assert force is False


def test_parse_args_missing_channel(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        publish.parse_args("|hello")
    assert "usage" in capsys.readouterr().err


def test_parse_args_missing_text(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        publish.parse_args("C0123456|")
    assert "usage" in capsys.readouterr().err


def test_parse_args_reads_file_prefix(tmp_path: Path) -> None:
    f = tmp_path / "msg.md"
    f.write_text("from a file", encoding="utf-8")
    channel, text, _ts, _force = publish.parse_args(f"C0123456|file://{f}")
    assert text == "from a file"


def test_parse_args_file_prefix_missing_file(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `file://` path inside the allowlist that simply does not exist --
    distinct from `test_parse_args_file_prefix_outside_allowlist` below,
    which never reaches the existence check at all."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "drafts").mkdir()
    with pytest.raises(SystemExit):
        publish.parse_args(f"C0123456|file://{tmp_path / 'drafts' / 'missing.md'}")
    assert "not found" in capsys.readouterr().err


def test_parse_args_file_prefix_outside_allowlist(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        publish.parse_args("C0123456|file:///no/such/file.md")
    assert "escapes the safety allowlist" in capsys.readouterr().err


# --- main() round trip, transport mocked at _api.call -----------------------

def test_main_posts_and_prints_ts(capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_call(method, token, *, params=None, body=None, timeout=15):
        calls.append((method, body or params or {}))
        if method == "chat.postMessage":
            return {"ok": True, "ts": "1700000000.000200", "channel": "C0123456"}
        return {"ok": False, "error": "no_permalink_in_test"}

    with mock.patch.object(publish, "call", fake_call), \
         mock.patch.object(publish, "get_bot_token", return_value="xoxb-fake"):
        publish.main("C0123456|Deploy finished")

    out = capsys.readouterr().out
    assert "ts=1700000000.000200" in out
    assert calls[0][0] == "chat.postMessage"
    assert calls[0][1]["channel"] == "C0123456"
    assert calls[0][1]["text"] == "Deploy finished"
    assert "thread_ts" not in calls[0][1]


def test_main_carries_thread_ts_into_the_post_body() -> None:
    calls: list[tuple[str, dict]] = []

    def fake_call(method, token, *, params=None, body=None, timeout=15):
        calls.append((method, body or params or {}))
        return {"ok": True, "ts": "2.0"}

    with mock.patch.object(publish, "call", fake_call), \
         mock.patch.object(publish, "get_bot_token", return_value="xoxb-fake"):
        publish.main("C0123456|reply|1.0")

    assert calls[0][1]["thread_ts"] == "1.0"


def test_main_prints_permalink_when_the_lookup_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    def fake_call(method, token, *, params=None, body=None, timeout=15):
        if method == "chat.postMessage":
            return {"ok": True, "ts": "3.0"}
        return {"ok": True, "permalink": "https://x.slack.com/archives/C1/p3"}

    with mock.patch.object(publish, "call", fake_call), \
         mock.patch.object(publish, "get_bot_token", return_value="xoxb-fake"):
        publish.main("C0123456|hi")

    assert "https://x.slack.com/archives/C1/p3" in capsys.readouterr().out


def test_main_exits_nonzero_when_slack_refuses(capsys: pytest.CaptureFixture[str]) -> None:
    def fake_call(method, token, *, params=None, body=None, timeout=15):
        return {"ok": False, "error": "channel_not_found"}

    with mock.patch.object(publish, "call", fake_call), \
         mock.patch.object(publish, "get_bot_token", return_value="xoxb-fake"):
        with pytest.raises(SystemExit):
            publish.main("C0123456|hi")
    assert "channel_not_found" in capsys.readouterr().err


def test_main_exits_nonzero_on_transport_failure(capsys: pytest.CaptureFixture[str]) -> None:
    def fake_call(method, token, *, params=None, body=None, timeout=15):
        raise publish.SlackTransportError("network: timed out")

    with mock.patch.object(publish, "call", fake_call), \
         mock.patch.object(publish, "get_bot_token", return_value="xoxb-fake"):
        with pytest.raises(SystemExit):
            publish.main("C0123456|hi")
    assert "timed out" in capsys.readouterr().err


def test_a_failed_permalink_lookup_does_not_undo_the_publish(capsys: pytest.CaptureFixture[str]) -> None:
    """The post already happened by the time the permalink lookup runs -- a
    failed lookup must not turn a successful publish into an error, and the
    ts already earned must still reach stdout."""
    def fake_call(method, token, *, params=None, body=None, timeout=15):
        if method == "chat.postMessage":
            return {"ok": True, "ts": "4.0"}
        raise publish.SlackTransportError("network: down")

    with mock.patch.object(publish, "call", fake_call), \
         mock.patch.object(publish, "get_bot_token", return_value="xoxb-fake"):
        publish.main("C0123456|hi")  # must not raise

    out = capsys.readouterr().out
    assert "ts=4.0" in out
    assert "URL:" not in out
