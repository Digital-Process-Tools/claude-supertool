"""Credential headers must not follow a redirect off their origin (#691, theme T6).

`urllib.request.urlopen` uses the default global opener. Its HTTPRedirectHandler
rebuilds a redirected request stripping only content-length/content-type, so
Authorization / api-key / Cookie travel to whatever host the Location names,
and http_error_302 permits an https->http downgrade. The redirect is followed
transparently, so the attacker response body is then returned to the caller as
though the real API had answered.

These tests are the attack: a loopback origin that answers 302 pointing at a
second loopback origin which records every header it receives. Tokens here are
obviously fake and are never printed.
"""
from __future__ import annotations

import importlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "presets"))

from _preset_loader import load_preset_module  # noqa: E402

# `_http` is the one shim shared by every preset, so it is imported plainly and
# deliberately left out of the loader's eviction list: all four clients must see
# the *same* module object for `_http._OPEN` to be a single seam. The clients
# themselves are loaded through the isolating loader, because a bare
# `import devto.comment` resolves its sibling `_auth` to whichever preset's
# same-named file got there first (docs/contributing.md, "Never reach a preset
# module by bare import").
_http = importlib.import_module("_http")
hn_gql = load_preset_module("hashnode", "_graphql", "rd_")
dv_rest = load_preset_module("devto", "_rest", "rd_")
dv_session = load_preset_module("devto", "_session", "rd_")
dv_comment = load_preset_module("devto", "comment", "rd_")
bs = load_preset_module("bluesky", "_atproto", "rd_")

FAKE_HASHNODE_TOKEN = "hn_FAKE_NOT_A_REAL_TOKEN_0001"
FAKE_DEVTO_KEY = "devto_FAKE_NOT_A_REAL_KEY_0002"
FAKE_DEVTO_COOKIE = "_forem_user=FAKE_NOT_A_REAL_SESSION_0003"
FAKE_JWT = "FAKE_NOT_A_REAL_JWT_0004"
FAKE_APP_PASSWORD = "fake-app-pass-0005"


# ---------------------------------------------------------------------------
# Loopback origins
# ---------------------------------------------------------------------------

class _Recorder(BaseHTTPRequestHandler):
    """Records the headers of every request, then either 302s once or answers 200."""

    def _run(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.server.received.append({k.lower(): v for k, v in self.headers.items()})
        target = self.server.redirect_to
        if target is not None:
            self.server.redirect_to = None  # one-shot, so a loop cannot hang the suite
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b'{"ok": true, "data": {"attacker": "controlled"}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _run

    def log_message(self, *args) -> None:
        pass


def _serve() -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", 0), _Recorder)
    srv.received = []
    srv.redirect_to = None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def hop():
    """front = the origin the client thinks it is calling; sink = the attacker.

    Same loopback address, advertised under a different host label, which is a
    different origin under scheme+host+port. The refusal path never connects to
    the sink, so `sink.received == []` is the assertion that carries the proof.
    """
    front = _serve()
    sink = _serve()
    ns = SimpleNamespace(
        front=front,
        sink=sink,
        front_base=f"http://127.0.0.1:{front.server_port}",
        sink_url=f"http://localhost:{sink.server_port}/landed",
    )
    front.redirect_to = ns.sink_url
    yield ns
    for srv in (front, sink):
        srv.shutdown()
        srv.server_close()


def assert_refused(hop, capsys) -> str:
    """Nothing reached the sink, and the refusal names the attempted destination."""
    assert hop.sink.received == [], "a request reached the off-origin destination"
    err = capsys.readouterr().err
    assert "refused off-origin redirect" in err, f"refusal not disclosed: {err!r}"
    assert f"localhost:{hop.sink.server_port}" in err, f"destination not named: {err!r}"
    return err


# ---------------------------------------------------------------------------
# 1. Origin policy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "src,dst",
    [
        ("https://api.example.com/a", "https://api.example.com/b"),
        ("https://api.example.com/a", "https://API.EXAMPLE.COM/b"),
        ("https://api.example.com:443/a", "https://api.example.com/b"),
        ("http://api.example.com/a", "http://api.example.com:80/b"),
        ("http://api.example.com/a", "https://api.example.com/b"),
        ("https://api.example.com:8443/a", "https://api.example.com:8443/b"),
    ],
)
def test_same_origin_redirects_are_allowed(src: str, dst: str) -> None:
    assert _http.check_redirect(src, dst) is None


@pytest.mark.parametrize(
    "src,dst,expect",
    [
        ("https://api.example.com/a", "https://evil.example/b", "different host"),
        ("https://api.example.com/a", "https://api.example.com.evil/b", "different host"),
        ("https://api.example.com/a", "https://sub.api.example.com/b", "different host"),
        ("https://api.example.com/a", "https://api.example.com:8443/b", "different port"),
        ("https://api.example.com/a", "http://api.example.com/b", "scheme downgrade"),
        ("http://api.example.com:8080/a", "https://api.example.com/b", "off the default ports"),
        ("https://api.example.com/a", "ftp://api.example.com/b", "not http/https"),
        ("https://api.example.com/a", "file:///etc/passwd", "not http/https"),
        ("https://api.example.com/a", "https://api.example.com:notaport/b", "unparseable port"),
    ],
)
def test_off_origin_redirects_are_refused(src: str, dst: str, expect: str) -> None:
    reason = _http.check_redirect(src, dst)
    assert reason is not None, f"{dst} should have been refused"
    assert expect in reason


def test_refusal_message_names_both_ends() -> None:
    exc = _http.RedirectRefused(
        "https://api.example.com/x", "https://evil.example/y", 302, "different host"
    )
    text = str(exc)
    assert "https://api.example.com/x" in text
    assert "https://evil.example/y" in text
    assert "302" in text
    assert "NOT followed" in text


def test_refusal_message_neutralises_a_hostile_destination() -> None:
    """The destination is copied from a remote `Location` header. Printed raw it
    could carry `\\r` or an ANSI CSI sequence and rewrite the warning about
    itself, so both URLs go through `repr`."""
    hostile = "https://api.example.com\r\n\x1b[2K\x1b[31mALL CLEAR\x1b[0m"
    text = str(_http.RedirectRefused("https://api.example.com/x", hostile, 302, "different host"))
    assert "\r" not in text, "a carriage return survived into the warning"
    assert "\n" not in text, "a newline survived into the warning"
    assert "\x1b" not in text, "an ANSI escape survived into the warning"
    assert "\\r\\n" in text, "the control characters should be shown, escaped"
    assert "ALL CLEAR" in text  # neutralised, not hidden — the operator still sees it


def test_redirect_refused_is_not_an_oserror() -> None:
    """gql_safe-style helpers catch OSError/URLError and return None. A refused
    redirect must not be absorbed into that silent path."""
    assert not issubclass(_http.RedirectRefused, OSError)
    assert not issubclass(_http.RedirectRefused, ValueError)


# ---------------------------------------------------------------------------
# 2. Every credentialed client refuses, loudly
# ---------------------------------------------------------------------------

def test_hashnode_gql_refuses(hop, capsys, monkeypatch) -> None:
    monkeypatch.setattr(hn_gql, "ENDPOINT", hop.front_base + "/gql")
    with pytest.raises(SystemExit) as exc:
        hn_gql.gql("query { x }", {}, FAKE_HASHNODE_TOKEN, timeout=5)
    assert exc.value.code != 0
    assert_refused(hop, capsys)


def test_hashnode_gql_safe_refuses_instead_of_returning_none(hop, capsys, monkeypatch) -> None:
    """gql_safe degrades to None on lookup failure. A credential exfiltration
    attempt is not a lookup failure — it must stop the run."""
    monkeypatch.setattr(hn_gql, "ENDPOINT", hop.front_base + "/gql")
    with pytest.raises(SystemExit) as exc:
        hn_gql.gql_safe("query { x }", {}, FAKE_HASHNODE_TOKEN, timeout=5)
    assert exc.value.code != 0
    assert_refused(hop, capsys)


def test_hashnode_refusal_message_scrubs_the_token(hop, capsys, monkeypatch) -> None:
    """The refusal prints two URLs. If the endpoint ever carried the token in its
    query string, the disclosure must not become the leak — this module scrubs
    the token from every other error path and this one is no exception."""
    monkeypatch.setattr(hn_gql, "ENDPOINT", f"{hop.front_base}/gql?t={FAKE_HASHNODE_TOKEN}")
    with pytest.raises(SystemExit):
        hn_gql.gql("query { x }", {}, FAKE_HASHNODE_TOKEN, timeout=5)
    err = capsys.readouterr().err
    assert FAKE_HASHNODE_TOKEN not in err
    assert "[REDACTED]" in err
    assert hop.sink.received == []


def test_devto_rest_refuses(hop, capsys, monkeypatch) -> None:
    monkeypatch.setattr(dv_rest, "BASE", hop.front_base + "/api")
    with pytest.raises(SystemExit) as exc:
        dv_rest.request("POST", "/articles", FAKE_DEVTO_KEY, body={"a": 1}, timeout=5)
    assert exc.value.code != 0
    assert_refused(hop, capsys)


def test_devto_session_csrf_fetch_refuses(hop, capsys, monkeypatch) -> None:
    monkeypatch.setattr(dv_session, "WEB_BASE", hop.front_base)
    with pytest.raises(SystemExit) as exc:
        dv_session.fetch_csrf_token(FAKE_DEVTO_COOKIE, timeout=5)
    assert exc.value.code != 0
    assert_refused(hop, capsys)


def test_devto_session_post_refuses(hop, capsys, monkeypatch) -> None:
    monkeypatch.setattr(dv_session, "WEB_BASE", hop.front_base)
    with pytest.raises(SystemExit) as exc:
        dv_session.web_post_json("/reactions", FAKE_DEVTO_COOKIE, "fake-csrf", {"x": 1}, timeout=5)
    assert exc.value.code != 0
    assert_refused(hop, capsys)


def test_bluesky_xrpc_refuses(hop, capsys, monkeypatch) -> None:
    monkeypatch.setattr(bs, "PDS", hop.front_base)
    with pytest.raises(SystemExit) as exc:
        bs.xrpc("app.bsky.feed.post", {"accessJwt": FAKE_JWT}, method="POST", body={"x": 1}, timeout=5)
    assert exc.value.code != 0
    assert_refused(hop, capsys)


def test_bluesky_create_session_refuses(hop, capsys, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bs, "PDS", hop.front_base)
    monkeypatch.setattr(bs, "SESSION_FILE", tmp_path / "session.json")
    with pytest.raises(SystemExit) as exc:
        bs.create_session("fake.handle.invalid", FAKE_APP_PASSWORD)
    assert exc.value.code != 0
    assert_refused(hop, capsys)


def test_bluesky_refresh_session_refuses_instead_of_returning_none(
    hop, capsys, monkeypatch, tmp_path
) -> None:
    """refresh_session returns None on failure so the caller falls back to
    create_session. A refused redirect must not vanish into that fallback."""
    monkeypatch.setattr(bs, "PDS", hop.front_base)
    monkeypatch.setattr(bs, "SESSION_FILE", tmp_path / "session.json")
    with pytest.raises(SystemExit) as exc:
        bs.refresh_session(FAKE_JWT)
    assert exc.value.code != 0
    assert_refused(hop, capsys)


# ---------------------------------------------------------------------------
# 3. The uncredentialed dev.to resolver discloses too, without hard-failing
# ---------------------------------------------------------------------------

def test_devto_comment_resolver_discloses_and_gives_up(hop, capsys, monkeypatch) -> None:
    """No credential rides on this hop, so it keeps its best-effort contract and
    returns None — but it names the destination it refused, rather than looking
    like an ordinary miss."""
    monkeypatch.setattr(dv_comment, "API_BASE", hop.front_base + "/api")
    assert dv_comment._resolve_parent_numeric_id("abc123", timeout=5) is None
    assert hop.sink.received == []
    out = capsys.readouterr()
    assert f"localhost:{hop.sink.server_port}" in (out.out + out.err)


# ---------------------------------------------------------------------------
# 4. Same-origin redirects still work (the guard is not "no redirects")
# ---------------------------------------------------------------------------

def test_same_origin_redirect_is_still_followed(hop, monkeypatch) -> None:
    hop.front.redirect_to = hop.front_base + "/api/elsewhere"
    monkeypatch.setattr(dv_rest, "BASE", hop.front_base + "/api")
    out = dv_rest.request("GET", "/articles/1", FAKE_DEVTO_KEY, timeout=5)
    assert out == {"ok": True, "data": {"attacker": "controlled"}}
    assert len(hop.front.received) == 2, "the same-origin redirect was not followed"
    assert hop.front.received[1].get("api-key") == FAKE_DEVTO_KEY


def test_a_permitted_redirect_is_still_disclosed(hop, capsys, monkeypatch) -> None:
    """Allowing a redirect is not the same as saying nothing about it.

    A followed redirect still changes which URL answered the question, and the
    caller cannot see it: `request()` returns a parsed body with no indication
    that a second URL produced it. That is the same substitution the refusal
    path exists to prevent, minus the credential theft.

    This is not hypothetical for these clients. dev.to really does answer
    `/settings` with a same-origin 302 to `/enter` once the session cookie has
    expired, and `fetch_csrf_token` then reports `authenticity_token not found
    in /settings HTML - Dev.to layout may have changed`. Every noun in that
    sentence is wrong: the HTML is `/enter`, not `/settings`, and the layout is
    fine - the cookie is dead. An operator acting reasonably on it goes looking
    for a dev.to redesign.
    """
    hop.front.redirect_to = hop.front_base + "/api/elsewhere"
    monkeypatch.setattr(dv_rest, "BASE", hop.front_base + "/api")
    dv_rest.request("GET", "/articles/1", FAKE_DEVTO_KEY, timeout=5)
    err = capsys.readouterr().err
    assert "redirected" in err, f"a followed redirect was not disclosed: {err!r}"
    assert "/articles/1" in err, f"origin URL not named: {err!r}"
    assert "/api/elsewhere" in err, f"destination URL not named: {err!r}"
    assert FAKE_DEVTO_KEY not in err, "the disclosure must not echo the credential"



# ---------------------------------------------------------------------------
# 5. Structural: the guard must be wired at every call site, not merely exist
# ---------------------------------------------------------------------------

def test_no_bare_urlopen_call_sites_remain_under_presets() -> None:
    """The headline failure mode of this repo security review is a guard that
    exists and is not wired everywhere. A fifth integration that types
    `urllib.request.urlopen(` re-earns this CVE, so fail here instead."""
    offenders = []
    for path in sorted((ROOT / "presets").rglob("*.py")):
        if path.name == "_http.py":
            continue
        text = path.read_text(encoding="utf-8")
        for num, line in enumerate(text.splitlines(), 1):
            if "urllib.request.urlopen(" in line and not line.lstrip().startswith("#"):
                offenders.append(f"{path.relative_to(ROOT)}:{num}")
    assert offenders == [], (
        "these call sites bypass presets/_http.py and will leak credentials "
        f"across a redirect: {offenders}"
    )


def test_shared_opener_replaces_the_default_redirect_handler() -> None:
    handlers = [type(h).__name__ for h in _http._OPENER.handlers]
    assert "SafeRedirectHandler" in handlers
    assert "HTTPRedirectHandler" not in handlers
