"""Integration half of #2042 for `slack_publish`: the disclosure marker
must reach the actual posted body (not just `_publish_safety`'s own unit
tests), and the confirmation preview a human sees before `|force` is not
supplied must show the disclosed text, not the bare one -- one of the
issue's own open questions, decided here as: yes, the preview shows what
will actually be attached.
"""
from __future__ import annotations

from unittest import mock

import pytest

from _preset_loader import load_preset_module


def _load():
    return load_preset_module("slack", "publish", "sldisc_")


publish = _load()


@pytest.fixture
def real_defaults(monkeypatch, tmp_path):
    """Undo conftest's suite-wide suppression so the on-by-default behavior
    is exercised for real, the same pattern `test_publish_disclosure_2042.
    py::strict_disclosure` uses."""
    monkeypatch.delenv("SUPERTOOL_NO_PUBLISH_DISCLOSURE", raising=False)
    monkeypatch.chdir(tmp_path)
    import _publish_safety
    if hasattr(_publish_safety, "_CACHED_CONFIG"):
        delattr(_publish_safety, "_CACHED_CONFIG")
    yield
    if hasattr(_publish_safety, "_CACHED_CONFIG"):
        delattr(_publish_safety, "_CACHED_CONFIG")


def test_the_posted_body_carries_the_marker_by_default(real_defaults) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_call(method, token, *, params=None, body=None, timeout=15):
        calls.append((method, body or {}))
        return {"ok": True, "ts": "1.0"}

    with mock.patch.object(publish, "call", fake_call), \
         mock.patch.object(publish, "get_bot_token", return_value="xoxb-fake"):
        publish.main("C0123456|Deploy finished")

    posted_text = calls[0][1]["text"]
    assert posted_text != "Deploy finished"
    assert "Deploy finished" in posted_text


def test_the_confirmation_preview_shows_the_disclosed_text(
    real_defaults, capsys, monkeypatch,
) -> None:
    """No `|force`, so `require_confirm` refuses and prints its preview --
    that preview must already contain the marker. Confirm's own suite-wide
    suppression is undone here (not in the shared fixture, which the
    "posted body" test above also uses and relies on staying suppressed so
    it can call `main()` without `|force` and still reach the post)."""
    monkeypatch.delenv("SUPERTOOL_NO_PUBLISH_CONFIRM", raising=False)
    with pytest.raises(SystemExit):
        publish.main("C0123456|Deploy finished")
    err = capsys.readouterr().err
    assert "Deploy finished" in err
    assert "[AI-generated]" in err


def test_disclosure_state_is_reported_when_suppressed(monkeypatch, capsys) -> None:
    """Suppression is a decision that must be findable on the receipt too --
    not just silently absent from the body."""
    monkeypatch.setenv("SUPERTOOL_NO_PUBLISH_DISCLOSURE", "1")

    def fake_call(method, token, *, params=None, body=None, timeout=15):
        return {"ok": True, "ts": "2.0"}

    with mock.patch.object(publish, "call", fake_call), \
         mock.patch.object(publish, "get_bot_token", return_value="xoxb-fake"):
        publish.main("C0123456|hi")

    out = capsys.readouterr().out
    assert "disclosure: suppressed" in out
