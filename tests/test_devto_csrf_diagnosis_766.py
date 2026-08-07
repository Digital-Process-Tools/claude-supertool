"""`fetch_csrf_token` must not report an expired cookie as a dev.to redesign (#766, item 4-narrow).

dev.to answers `/settings` with a **same-origin** 302 to `/enter` once the
session cookie expires. That hop is legitimate and `_http.urlopen` follows it,
so the HTML that comes back is the login page — `_AUTH_TOKEN_RE` finds nothing
and the op reports:

    authenticity_token not found in /settings HTML — Dev.to layout may have changed

Every noun in that line can be wrong: the HTML is `/enter`, the layout is fine,
the cookie is dead. The generic redirect NOTE from #761 discloses the hop but
does not correct the diagnosis, and the diagnosis is the line an operator acts
on.

The bug is this repo's house special: a *state that was never checked* reported
as a confident finding. The fix is three states, not two — token found, token
absent from a page that answered under a different URL (cookie), token absent
from the page actually asked for (unknown, possibly the layout). The third one
must stay unknown: replacing "layout changed" with "cookie expired" everywhere
would just move the misdiagnosis.
"""
from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "presets"))
sys.path.insert(0, str(ROOT / "tests"))

from _no_fqdn_server import NoFqdnHTTPServer  # noqa: E402
from _preset_loader import load_preset_module  # noqa: E402

dv_session = load_preset_module("devto", "_session", "csrf_")

FAKE_DEVTO_COOKIE = "_forem_user=FAKE_NOT_A_REAL_SESSION_0766"

LOGIN_HTML = b"<html><body><h1>Log in</h1><form action='/enter'></form></body></html>"
SETTINGS_HTML = (
    b'<html><body><form><input name="authenticity_token" value="tok-from-settings">'
    b"</form></body></html>"
)
REDESIGNED_HTML = b"<html><body><div id='settings-app'>no rails form here</div></body></html>"


class _Devto(BaseHTTPRequestHandler):
    """`/settings` behaves as the server's `settings_body` says; `/enter` is the login page."""

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/enter"):
            return self._send(LOGIN_HTML)
        body = self.server.settings_body
        if body is None:  # the expired-cookie shape: same-origin 302 to the login page
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/enter")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send(body)

    def _send(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def devto(monkeypatch):
    srv = NoFqdnHTTPServer(("127.0.0.1", 0), _Devto)
    srv.settings_body = SETTINGS_HTML
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(dv_session, "WEB_BASE", f"http://127.0.0.1:{srv.server_port}")
    yield srv
    srv.shutdown()
    srv.server_close()


def test_token_is_returned_when_settings_answers_settings(devto) -> None:
    """The ordinary path, unchanged: a live cookie gets the token."""
    assert dv_session.fetch_csrf_token(FAKE_DEVTO_COOKIE, timeout=5) == "tok-from-settings"


def test_expired_cookie_is_named_as_the_cause_not_a_redesign(devto, capsys) -> None:
    """`/settings` -> `/enter`: the page that answered is not the page asked for.

    That is a checkable fact and it fully explains the missing token, so the
    error must say it. Asserting the absence of the layout claim is the half
    that would fail if the code did nothing.
    """
    devto.settings_body = None  # 302 to /enter
    with pytest.raises(SystemExit) as exc:
        dv_session.fetch_csrf_token(FAKE_DEVTO_COOKIE, timeout=5)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "/enter" in err, f"the URL that actually answered is not named: {err!r}"
    assert "cookie" in err.lower(), f"the expired cookie is not named as the cause: {err!r}"
    assert "layout" not in err.lower(), (
        "a redirected response still blames a dev.to redesign, which is the "
        f"misdiagnosis #766 is about: {err!r}"
    )


def test_no_redirect_keeps_the_layout_diagnosis_as_the_unknown_it_is(devto, capsys) -> None:
    """The other direction of the same rule: do not trade one confident wrong
    answer for another. When `/settings` itself answered and the token is
    absent, the cookie is demonstrably fine and the cause really is unknown."""
    devto.settings_body = REDESIGNED_HTML
    with pytest.raises(SystemExit) as exc:
        dv_session.fetch_csrf_token(FAKE_DEVTO_COOKIE, timeout=5)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "layout" in err.lower(), f"an unexplained miss should stay unexplained: {err!r}"
    assert "no redirect" in err.lower(), (
        f"the check that was actually run should be visible in the message: {err!r}"
    )
    # Ruling the cookie *out* is the point; asserting the word "expired" is
    # absent would forbid saying "not an expired session", which is the useful
    # half. What must be absent is the affirmative blame.
    assert "cookie is dead" not in err.lower(), (
        f"a non-redirected miss must not be blamed on the cookie either: {err!r}"
    )
