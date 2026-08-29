"""Tests for #2074: `fetch_permalink` collapsed a transport error, a
Slack-level refusal, and a genuinely absent permalink into the same `""`,
so the caller's `if permalink` print rendered all three as identical
silence. Fixed by returning `(permalink, note)`, so `main()` can print a
distinguishable line for each failure shape instead of nothing at all.
"""
from __future__ import annotations

from unittest import mock

from _preset_loader import load_preset_module


def _load():
    return load_preset_module("slack", "publish", "sl2074_")


publish = _load()


def test_fetch_permalink_returns_note_on_transport_error() -> None:
    def fake_call(method, token, *, params=None, body=None, timeout=15):
        raise publish.SlackTransportError("network: timed out")

    with mock.patch.object(publish, "call", fake_call):
        permalink, note = publish.fetch_permalink("C1", "1.0", "xoxb-fake")
    assert permalink == ""
    assert "timed out" in note


def test_fetch_permalink_returns_note_on_slack_refusal() -> None:
    def fake_call(method, token, *, params=None, body=None, timeout=15):
        return {"ok": False, "error": "channel_not_found"}

    with mock.patch.object(publish, "call", fake_call):
        permalink, note = publish.fetch_permalink("C1", "1.0", "xoxb-fake")
    assert permalink == ""
    assert "channel_not_found" in note


def test_fetch_permalink_returns_note_when_slack_answers_ok_with_no_permalink() -> None:
    def fake_call(method, token, *, params=None, body=None, timeout=15):
        return {"ok": True}  # no "permalink" key at all -- genuinely absent

    with mock.patch.object(publish, "call", fake_call):
        permalink, note = publish.fetch_permalink("C1", "1.0", "xoxb-fake")
    assert permalink == ""
    assert note
    assert "timed out" not in note
    assert "channel_not_found" not in note


def test_the_three_failure_notes_are_pairwise_distinguishable() -> None:
    """Positive control: it is not enough that each failure returns SOME
    note -- they must not collapse back into one shared sentence, which
    would just move the old ambiguity from the empty string to a shared
    note. The bar this test enforces: a run that returned the same generic
    note for all three inputs must fail here."""
    def fake_transport(method, token, *, params=None, body=None, timeout=15):
        raise publish.SlackTransportError("boom")

    def fake_refusal(method, token, *, params=None, body=None, timeout=15):
        return {"ok": False, "error": "channel_not_found"}

    def fake_absent(method, token, *, params=None, body=None, timeout=15):
        return {"ok": True}

    with mock.patch.object(publish, "call", fake_transport):
        _, note_transport = publish.fetch_permalink("C1", "1.0", "xoxb-fake")
    with mock.patch.object(publish, "call", fake_refusal):
        _, note_refusal = publish.fetch_permalink("C1", "1.0", "xoxb-fake")
    with mock.patch.object(publish, "call", fake_absent):
        _, note_absent = publish.fetch_permalink("C1", "1.0", "xoxb-fake")

    notes = {note_transport, note_refusal, note_absent}
    assert len(notes) == 3, f"failure notes collapsed: {notes!r}"


def test_main_renders_a_distinguishable_line_for_each_permalink_failure(capsys) -> None:
    def make_call(permalink_resp):
        def fake_call(method, token, *, params=None, body=None, timeout=15):
            if method == "chat.postMessage":
                return {"ok": True, "ts": "9.0"}
            return permalink_resp
        return fake_call

    with mock.patch.object(publish, "call", make_call({"ok": False, "error": "channel_not_found"})), \
         mock.patch.object(publish, "get_bot_token", return_value="xoxb-fake"):
        publish.main("C0123456|hi")
    refusal_out = capsys.readouterr().out
    assert "URL:" not in refusal_out
    assert "channel_not_found" in refusal_out

    def raising_call(method, token, *, params=None, body=None, timeout=15):
        if method == "chat.postMessage":
            return {"ok": True, "ts": "9.1"}
        raise publish.SlackTransportError("timed out")

    with mock.patch.object(publish, "call", raising_call), \
         mock.patch.object(publish, "get_bot_token", return_value="xoxb-fake"):
        publish.main("C0123456|hi")
    transport_out = capsys.readouterr().out
    assert "URL:" not in transport_out
    assert "timed out" in transport_out

    with mock.patch.object(publish, "call", make_call({"ok": True})), \
         mock.patch.object(publish, "get_bot_token", return_value="xoxb-fake"):
        publish.main("C0123456|hi")
    absent_out = capsys.readouterr().out
    assert "URL:" not in absent_out

    assert refusal_out != transport_out
    assert transport_out != absent_out
    assert refusal_out != absent_out
