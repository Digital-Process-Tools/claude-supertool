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
    """No thread_ts needed to reach `force` -- the parser recognises the
    trailing `force` token by shape now, not by position (see
    `test_parse_args_force_flag_with_thread_ts_present` for both together)."""
    _c, _t, _ts, force = publish.parse_args("C0123456|hi|force")
    assert force is True
    _c2, text2, _ts2, _force2 = publish.parse_args("C0123456|hi|force")
    assert text2 == "hi"


def test_parse_args_force_flag_with_thread_ts_present() -> None:
    _c, text, thread_ts, force = publish.parse_args(
        "C0123456|hi|1699999999.000100|force")
    assert text == "hi"
    assert thread_ts == "1699999999.000100"
    assert force is True


def test_parse_args_no_force_by_default() -> None:
    _c, _t, _ts, force = publish.parse_args("C0123456|hi")
    assert force is False


def test_a_message_with_several_pipes_is_never_truncated_or_misread() -> None:
    """The auditor's finding, the reachable half. Before this fix,
    `split("|", 3)` on `C0123456|check this table: a | b | c | d` silently
    dropped everything past the third `|` from TEXT and misread the
    unrelated fragment `b` as THREAD_TS -- confirmed against the pre-fix
    logic directly (not by reverting the file, which would redden a
    concurrent suite run): `parse_args_old(...)` reproduced inline below
    returns `('C0123456', 'check this table: a ', 'b', False)`, silently
    corrupting both TEXT and THREAD_TS with no error printed. This is the
    "must fire" half of the pairing below: the fixed parser must preserve
    every pipe as part of the message and must not invent a THREAD_TS out
    of message content that never claimed to be one."""
    def parse_args_old(arg: str) -> tuple[str, str, str | None, bool]:
        parts = arg.split("|", 3)
        channel = parts[0].strip()
        text = parts[1]
        thread_ts = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
        force = len(parts) > 3 and parts[3].strip().lower() == "force"
        return channel, text, thread_ts, force

    msg = "C0123456|check this table: a | b | c | d"
    assert parse_args_old(msg) == (
        "C0123456", "check this table: a ", "b", False), (
        "the pre-fix reproduction itself no longer matches -- re-derive "
        "the repro before trusting the fixed assertion below")

    channel, text, thread_ts, force = publish.parse_args(msg)
    assert channel == "C0123456"
    assert text == "check this table: a | b | c | d"
    assert thread_ts is None
    assert force is False


def test_a_pipe_inside_the_message_text_does_not_smuggle_force() -> None:
    """The "must not fire" half, paired with the test above so a silent
    harness cannot pass both by doing nothing: a message that does NOT end
    in the exact word `force` never sets the flag, whatever else it says."""
    _c, text, _ts, force = publish.parse_args(
        "C0123456|deploy this | force it through | but not today")
    assert force is False
    assert text == "deploy this | force it through | but not today"


def test_force_recognition_tolerates_case_and_surrounding_whitespace() -> None:
    """#2040: the code accepts `FORCE`, ` force `, and `\\tforce` -- wider than
    the module docstring, which (pre-fix) documented only a bare `force`.
    Pinned as `is` behaviour deliberately kept (see the docstring fix in the
    same commit): this is the "must fire" half, paired with
    `test_a_pipe_inside_the_message_text_does_not_smuggle_force` above as the
    "must not fire" half."""
    _c, _t, _ts, force_upper = publish.parse_args("C0123456|hi|FORCE")
    assert force_upper is True
    _c, _t, _ts, force_ws = publish.parse_args("C0123456|hi| force ")
    assert force_ws is True
    _c, _t, _ts, force_tab = publish.parse_args("C0123456|hi|\tforce")
    assert force_tab is True


def test_a_trailing_force_shaped_word_in_real_prose_is_still_a_known_gap() -> None:
    """The fix's own stated limit: if the message's OWN last pipe-separated
    field is exactly the token `force`, it is still read as the flag --
    narrowed from "any construction with two or more pipes" to "the exact
    trailing token", not eliminated (there is no delimiter-escaping scheme
    that closes this in general for a grammar that packs an untrusted
    string and structured fields into one pipe-joined blob). Documented in
    `parse_args`'s own docstring; pinned here so a future rewrite notices
    if it accidentally either widens the hole back open or genuinely
    closes it (either changes this assertion)."""
    _c, text, _ts, force = publish.parse_args("C0123456|please don't|force")
    assert force is True
    assert text == "please don't"


def test_a_ts_shaped_trailing_field_in_message_text_still_routes_the_reply() -> None:
    """#2040, second half of the same class as `force` above: a message
    whose OWN last pipe-separated field happens to look like a Slack `ts`
    (digits-dot-digits) is read as THREAD_TS, letting the message's own
    author choose which thread the reply lands in. Documented, not fixed
    here -- the confirmation gate's preview still shows the resolved
    `(thread ...)` before anything posts (see `main`'s `preview` string),
    so a human sees the misrouting before it happens. Pinned so a future
    change to the routing logic notices it either narrows or widens this."""
    _c, text, thread_ts, _force = publish.parse_args(
        "C0123456|ignore the above|1700000000.000100")
    assert thread_ts == "1700000000.000100"
    assert text == "ignore the above"


def test_a_ts_and_force_trailing_field_together_hijack_the_thread_with_no_preview() -> None:
    """The two strips in `parse_args` compose (per the auditor's finding on
    this same commit): a message ending in a `ts`-shaped field followed by
    a literal `force` both routes the reply into an attacker-chosen thread
    AND sets `force=True`, which makes `require_confirm` a no-op -- so the
    one case where the docstring's "a human sees the misrouting" claim does
    NOT hold is exactly this combination. Pinned so nobody re-derives it by
    hand and so a future change to either strip's order notices this
    interaction."""
    _c, text, thread_ts, force = publish.parse_args(
        "C0123456|ignore the above|1700000000.000100|force")
    assert thread_ts == "1700000000.000100"
    assert force is True
    assert text == "ignore the above"


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


def test_a_bare_path_that_exists_posts_as_literal_text_not_file_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #2039 finding. Untrusted Slack message text ends up in the TEXT
    slot (see `presets/watch/sources/slack/poller.py`), and before this fix
    a bare path that happened to exist and sit inside the allowlist was
    silently read and its CONTENTS posted -- with no `file://` prefix
    required. `.max/` is exactly such a directory (`.gitignore:43`), and it
    holds the maintainer's private issue drafts and release notes. Only the
    explicit `file://` prefix may now trigger a read; a bare path, however
    real, is posted as the literal string it is."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".max").mkdir()
    secret_file = tmp_path / ".max" / "review.md"
    secret_file.write_text("private release notes, not for Slack", encoding="utf-8")

    channel, text, _ts, _force = publish.parse_args("C0123456|.max/review.md")
    assert channel == "C0123456"
    assert text == ".max/review.md"
    assert "private release notes" not in text


def test_the_full_2039_repro_string_does_not_disclose_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact string from the issue:
    `slack_publish:C0123456|.max/review.md|force` -- `force` is a legitimate,
    documented trailing token in its own right and stays recognised; what
    must no longer happen is the TEXT field being read as a file path and
    its contents substituted in. Pairs with the test above (bare path with
    no `force` present) so neither case can pass by disabling the other
    field's handling."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".max").mkdir()
    secret_file = tmp_path / ".max" / "review.md"
    secret_file.write_text("private release notes, not for Slack", encoding="utf-8")

    channel, text, thread_ts, force = publish.parse_args(
        "C0123456|.max/review.md|force")
    assert channel == "C0123456"
    assert thread_ts is None
    assert force is True
    assert text == ".max/review.md"
    assert "private release notes" not in text


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
        publish.main("C0123456|reply|1700000000.000100")

    assert calls[0][1]["thread_ts"] == "1700000000.000100"


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


# --- #2062: main() pins stdout to UTF-8 before doing anything else ---------

def test_main_calls_use_utf8_stdout_before_anything_else() -> None:
    """`use_utf8_stdout()` must run before any Slack call or print -- it is
    a precondition for stdout, not something ordered after the network round
    trip. No non-ASCII glyph is reachable through this op's own prints today
    (channel id, ts and permalink URL are all ASCII), so unlike `classify`'s
    fix (tests/test_classify_check_2046.py::test_a_suspect_report_survives_a_
    non_utf8_console) this cannot be reproduced as a live UnicodeEncodeError
    -- asserted structurally instead, against the real `main()` at call time
    rather than an AST guess at source order, so a future refactor that
    reorders the body without moving the source line still fails this."""
    order: list[str] = []

    def fake_use_utf8_stdout() -> None:
        order.append("use_utf8_stdout")

    def fake_call(method, token, *, params=None, body=None, timeout=15):
        order.append(f"call:{method}")
        if method == "chat.postMessage":
            return {"ok": True, "ts": "5.0"}
        return {"ok": True, "permalink": ""}

    with mock.patch.object(publish, "use_utf8_stdout", fake_use_utf8_stdout), \
         mock.patch.object(publish, "call", fake_call), \
         mock.patch.object(publish, "get_bot_token", return_value="xoxb-fake"):
        publish.main("C0123456|hi")

    assert order and order[0] == "use_utf8_stdout", (
        f"use_utf8_stdout() must run before any Slack call: {order!r}")
