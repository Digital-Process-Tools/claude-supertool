"""Remote error bodies must not replay our own credentials (#691, theme T5).

`devto`, `bluesky` and `hashnode` all echo up to 200 characters of the remote
error body so a failure says something useful. None of that is wrong — but the
body is written by the other end, and the other end is free to quote the request
back at us. `bluesky._atproto.create_session` is the sharp case: its request
body *is* the app password, so a PDS or proxy that echoes the request replayed
the password onto stderr.

The existing security tests for these presets were structurally blind to this:
every one of them fed an error body that did not contain the credential, so a
formatter with no scrubbing at all passed them. Each test below feeds a body
that *does*.

hashnode already had `_scrub_token`, applied by the caller to the formatted
string — after the body had been cut to 200 characters. A token straddling that
cut survived as a fragment, because the fragment is not the string `replace()`
is looking for. That ordering is the third test class here.

Every credential in this file is a fake constant defined at the top.
"""
from __future__ import annotations

import importlib.util
import io
import sys
import urllib.error
from pathlib import Path

import pytest

from _netblock import block_outbound


@pytest.fixture(autouse=True)
def _no_outbound(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests stub their transport. Prove it (#1312).

    Two of them did not, and could not: they patched
    `MODULE.urllib.request.urlopen`, but every preset here routes through
    `_http.urlopen`, which calls `_http._OPEN` -- bound to the opener at import
    and never re-read from `urllib.request`. The stub therefore replaced a name
    nothing calls, the request went to the live host, and both tests passed on
    whatever the internet said. `test_create_session_does_not_print_the_password`
    is the sharp one: its assertion is that a *credential echoed by the remote*
    is redacted, and the echo it injects had never once been delivered.

    Redundant since #1341: `tests/conftest.py` arms `block_outbound` for every
    test in the suite, so this file is no longer what guards it and deleting
    this fixture would reopen nothing. It is kept for the history above, which
    is about *these* two tests and belongs beside them.
    """
    block_outbound(monkeypatch)


FAKE_APP_PASSWORD = "fake-abcd-efgh-ijkl-mnop"
FAKE_API_KEY = "FAKE_devto_api_key_0123456789"
FAKE_TOKEN = "FAKE_hashnode_token_0123456789"

_PRESETS = Path(__file__).resolve().parent.parent / "presets"


def _load(name: str, relpath: str):
    """Import a preset helper module by path — presets are not a package."""
    spec = importlib.util.spec_from_file_location(name, _PRESETS / relpath)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


atproto = _load("_t5_atproto", "bluesky/_atproto.py")
devto_rest = _load("_t5_devto_rest", "devto/_rest.py")
hashnode_gql = _load("_t5_hashnode_gql", "hashnode/_graphql.py")


def _http_error(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.invalid/x", code, "Reason", {},
        io.BytesIO(body.encode("utf-8")),
    )


class TestBlueskyErrorEcho:
    def test_an_echoed_app_password_is_redacted(self) -> None:
        body = (
            '{"error":"InvalidRequest","message":"bad request: '
            '{\\"identifier\\":\\"me.bsky.social\\",'
            f'\\"password\\":\\"{FAKE_APP_PASSWORD}\\"}}"}}'
        )
        out = atproto._format_http_error(_http_error(400, body), FAKE_APP_PASSWORD)
        assert FAKE_APP_PASSWORD not in out
        assert "[REDACTED]" in out

    def test_an_echoed_access_jwt_is_redacted(self) -> None:
        jwt = "eyJfake.eyJfakepayload.fakesignature"
        out = atproto._format_http_error(_http_error(403, f"token {jwt} rejected"), jwt)
        assert jwt not in out

    def test_an_ordinary_body_still_reaches_the_operator(self) -> None:
        out = atproto._format_http_error(
            _http_error(400, "record must have a text field"), FAKE_APP_PASSWORD)
        assert "record must have a text field" in out

    def test_create_session_does_not_print_the_password(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """End to end through the function whose request body is the password."""
        def _boom(req, timeout=0):
            raise _http_error(
                400, f'{{"message":"got password {FAKE_APP_PASSWORD}"}}')

        # `_http._OPEN` is the seam, not `urllib.request.urlopen` (#1312).
        # `_atproto` calls `_http.urlopen`, which calls `_OPEN` -- bound to the
        # opener at import and never looked up on `urllib.request` again. The
        # old target was never consulted, so this test made a live request to
        # bsky.social and asserted a redaction against a body it did not write.
        monkeypatch.setattr(sys.modules["_http"], "_OPEN", _boom)
        with pytest.raises(SystemExit):
            atproto.create_session("me.bsky.social", FAKE_APP_PASSWORD)
        err = capsys.readouterr().err
        assert FAKE_APP_PASSWORD not in err
        assert "createSession" in err
        # Proof the injected body was delivered. Without it the two assertions
        # above are satisfied by *any* failure, including the connection error
        # this test used to produce (#1312).
        assert "[REDACTED]" in err


class TestDevtoErrorEcho:
    def test_an_echoed_api_key_is_redacted(self) -> None:
        body = f'{{"error":"unprocessable","headers":{{"api-key":"{FAKE_API_KEY}"}}}}'
        out = devto_rest._format_http_error(_http_error(422, body), FAKE_API_KEY)
        assert FAKE_API_KEY not in out
        assert "[REDACTED]" in out

    def test_the_generic_branch_is_scrubbed_too(self) -> None:
        out = devto_rest._format_http_error(
            _http_error(500, f"upstream said {FAKE_API_KEY}"), FAKE_API_KEY)
        assert FAKE_API_KEY not in out

    def test_a_bad_json_body_is_scrubbed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        class _Resp:
            """Enough of a response for `_http.read_capped` to drain it.

            `read` takes a size and returns b"" when exhausted -- `read_capped`
            calls it in a loop and would otherwise never terminate. No
            `headers`, so no declared Content-Length, so no short-read check.
            """

            def __init__(self) -> None:
                self._body = f"<html>{FAKE_API_KEY}</html>".encode("utf-8")

            def __enter__(self): return self
            def __exit__(self, *a): return False
            def close(self) -> None: pass

            def read(self, n: int = -1) -> bytes:
                out, self._body = self._body, b""
                return out

        # `_http._OPEN`, not `urllib.request.urlopen` -- see the bluesky case
        # above and the module fixture (#1312).
        monkeypatch.setattr(
            sys.modules["_http"], "_OPEN", lambda req, timeout=0: _Resp())
        with pytest.raises(SystemExit):
            devto_rest.request("GET", "/articles/1", FAKE_API_KEY)
        err = capsys.readouterr().err
        assert FAKE_API_KEY not in err
        # Proof the injected body was delivered, not merely that some unrelated
        # failure printed no key (#1312).
        assert "<html>" in err and "[REDACTED]" in err


class TestHashnodeTruncationOrder:
    def test_a_token_straddling_the_truncation_boundary_is_redacted(self) -> None:
        """The bug the existing scrubber had: the caller scrubbed the formatted
        string, by which point the body was already cut to 200 chars, so only a
        prefix of the token remained and `replace()` no longer matched it."""
        # 171 is the worst case for this 30-char token against the 200-char
        # cut: it leaves 29 of the token's 30 characters inside the truncated
        # body, and the unfixed code leaked exactly those 29. The padding here
        # was originally 190, which leaks only 10 — so the `[:12]` assertion
        # that went with it PASSED against the broken code and pinned nothing.
        padding = "x" * 171
        out = hashnode_gql._format_http_error(
            _http_error(500, padding + FAKE_TOKEN + "y" * 50), FAKE_TOKEN)
        assert FAKE_TOKEN not in out
        assert FAKE_TOKEN[:29] not in out

    def test_a_wholly_contained_token_is_still_redacted(self) -> None:
        out = hashnode_gql._format_http_error(
            _http_error(500, f"bad auth {FAKE_TOKEN}"), FAKE_TOKEN)
        assert FAKE_TOKEN not in out


class TestScrubBoundaries:
    def test_an_empty_secret_changes_nothing(self) -> None:
        assert atproto._scrub("hello", "") == "hello"

    def test_a_very_short_secret_is_not_used_to_redact(self) -> None:
        """Redacting on a 2-char value would rewrite ordinary prose and make the
        error unreadable — a worse outcome than the one being prevented."""
        assert atproto._scrub("a common word", "a") == "a common word"

    def test_multiple_secrets_are_all_redacted(self) -> None:
        out = atproto._scrub("one FAKE_aaaaaa two FAKE_bbbbbb", "FAKE_aaaaaa", "FAKE_bbbbbb")
        assert "FAKE_aaaaaa" not in out and "FAKE_bbbbbb" not in out
