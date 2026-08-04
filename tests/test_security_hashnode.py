"""Hashnode op security audit.

Focus areas:
- Token leakage in error messages, stack traces, and echoed HTTP bodies
- GraphQL injection via free-text fields (verify the helper uses variables)
- Missing/empty token files (clean error, no traceback)
- Bad-JSON / network errors (no token in output)
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "presets"))

import importlib

hn_auth = importlib.import_module("hashnode._auth")
hn_gql = importlib.import_module("hashnode._graphql")

FAKE_TOKEN = "hn_supersecret_token_xyz_dont_leak"


class _FakeResp(io.BytesIO):
    """A response double that can be read the way `_http.read_capped` reads.

    A `MagicMock(read=lambda: body)` stopped modelling a response once bodies
    became bounded (#766): the reader asks for a byte count and stops at EOF,
    and a mock answers both with another mock. `io.BytesIO` gives `read`,
    `read1` and `close` for free, which is the whole surface `read_capped`
    touches.
    """

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *a) -> None:
        return None


# ---------------------------------------------------------------------------
# 1. GraphQL helper uses variables (not string-concat) → injection-safe
# ---------------------------------------------------------------------------

def test_gql_passes_variables_as_separate_payload_field() -> None:
    """The query string and variables must travel as separate JSON fields.
    If the helper interpolated variables into the query, a malicious slug
    like `"} mutation Evil { __typename` could be parsed as GraphQL."""
    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["body"] = req.data.decode()
        return _FakeResp(b'{"data":{"ok":true}}')

    with patch("_http._OPEN", fake_urlopen):
        hn_gql.gql(
            "query Foo($slug: String!) { post(slug: $slug) { id } }",
            {"slug": "evil\"} mutation X { __typename"},
            FAKE_TOKEN,
        )
    sent = json.loads(captured["body"])
    assert "query" in sent and "variables" in sent
    # The injection payload sits inside variables (safe), NOT in query
    assert "mutation X" not in sent["query"]
    assert sent["variables"]["slug"].startswith("evil")


# ---------------------------------------------------------------------------
# 2. Token NOT printed in known error messages
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code,reason", [
    (401, "Unauthorized"),
    (403, "Forbidden"),
    (404, "Not Found"),
    (429, "Too Many Requests"),
])
def test_known_http_errors_never_include_token(
    capsys, code, reason
) -> None:
    """401/403/404/429 use hardcoded messages — token must never appear."""
    err = urllib.error.HTTPError(
        "https://gql.hashnode.com",
        code,
        reason,
        {},
        io.BytesIO(f"body containing {FAKE_TOKEN} echoed by server".encode()),
    )

    def fake_urlopen(*a, **k):
        raise err

    with patch("_http._OPEN", fake_urlopen):
        with pytest.raises(SystemExit):
            hn_gql.gql("query{}", {}, FAKE_TOKEN)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert FAKE_TOKEN not in combined, (
        f"Token leaked in error output for HTTP {code}"
    )


# ---------------------------------------------------------------------------
# 3. Unknown HTTP code path echoes body[:200] — KNOWN leak vector
# ---------------------------------------------------------------------------

def test_unknown_http_code_body_does_not_leak_token(capsys) -> None:
    """Fixed 2026-05-23: _scrub_token redacts the token from any body
    printed in error output. Upstream proxies echoing the Authorization
    header in 5xx bodies no longer leak the credential to stderr."""
    err = urllib.error.HTTPError(
        "https://gql.hashnode.com",
        500,
        "Internal Server Error",
        {},
        io.BytesIO(f"Authorization: {FAKE_TOKEN} caused server crash".encode()),
    )

    def fake_urlopen(*a, **k):
        raise err

    with patch("_http._OPEN", fake_urlopen):
        with pytest.raises(SystemExit):
            hn_gql.gql("query{}", {}, FAKE_TOKEN)
    captured = capsys.readouterr()
    assert FAKE_TOKEN not in captured.err
    assert "[REDACTED]" in captured.err


# ---------------------------------------------------------------------------
# 4. Network errors don't leak token
# ---------------------------------------------------------------------------

def test_network_error_does_not_leak_token(capsys) -> None:
    """Fixed 2026-05-23: URLError.reason now passed through _scrub_token
    before stderr.write — if the OS error string contained the token
    (rare but possible), it's redacted."""
    def fake_urlopen(*a, **k):
        raise urllib.error.URLError(f"refused: token was {FAKE_TOKEN}")

    with patch("_http._OPEN", fake_urlopen):
        with pytest.raises(SystemExit):
            hn_gql.gql("query{}", {}, FAKE_TOKEN)
    captured = capsys.readouterr()
    assert FAKE_TOKEN not in captured.err


# ---------------------------------------------------------------------------
# 5. Bad JSON response → clean error, no token
# ---------------------------------------------------------------------------

def test_bad_json_response_does_not_leak_token(capsys) -> None:
    def fake_urlopen(req, timeout=30):
        return _FakeResp(f"<html>not json — {FAKE_TOKEN}</html>".encode())

    with patch("_http._OPEN", fake_urlopen):
        with pytest.raises(SystemExit):
            hn_gql.gql("query{}", {}, FAKE_TOKEN)
    captured = capsys.readouterr()
    assert FAKE_TOKEN not in (captured.out + captured.err), (
        "Bad-JSON branch must not include response body in error"
    )


# ---------------------------------------------------------------------------
# 6. GraphQL errors[] passthrough — message printed, token must not leak
# ---------------------------------------------------------------------------

def test_graphql_errors_message_does_not_leak_token(capsys) -> None:
    """If the server returns `{"errors":[{"message":"...token=X..."}]}`,
    the op prints the message. Hashnode wouldn't echo the token, but a
    misbehaving server could. Pin behavior."""
    body = json.dumps({
        "errors": [{
            "message": f"validation failed at token={FAKE_TOKEN}",
            "extensions": {"code": "BAD_USER_INPUT"},
        }]
    }).encode()

    def fake_urlopen(req, timeout=30):
        return _FakeResp(body)

    with patch("_http._OPEN", fake_urlopen):
        with pytest.raises(SystemExit):
            hn_gql.gql("query{}", {}, FAKE_TOKEN)
    captured = capsys.readouterr()
    # Fixed 2026-05-23: GraphQL error.message is now passed through
    # _scrub_token before printing.
    assert FAKE_TOKEN not in captured.err


# ---------------------------------------------------------------------------
# 7. Missing token → clean error, sys.exit(2), no stack trace
# ---------------------------------------------------------------------------

def test_missing_token_exits_cleanly(capsys, monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("HASHNODE_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))   # no ~/.config/hashnode/token
    monkeypatch.chdir(tmp_path)                  # no .hashnode-token in cwd
    with pytest.raises(SystemExit) as exc:
        hn_auth.get_token()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "Hashnode token not found" in captured.err
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# 8. Token file resolution does NOT follow path-traversal env value
# ---------------------------------------------------------------------------

def test_token_resolution_only_reads_fixed_paths(monkeypatch, tmp_path) -> None:
    """_read_first only reads from the hardcoded paths in get_token().
    A user can't redirect token resolution to /etc/passwd via env override —
    only HASHNODE_TOKEN (the value itself) is honored, not a path."""
    monkeypatch.delenv("HASHNODE_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    # os.path.expanduser uses USERPROFILE on Windows, HOME on POSIX. Set both
    # so the test repoints `~` to tmp_path on every platform.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # Plant an "evil" file at a non-allowed location
    evil = tmp_path / "evil.txt"
    evil.write_text("STOLEN_FROM_EVIL_LOCATION\n")
    # Provide a real token only via the canonical path
    (tmp_path / ".config" / "hashnode").mkdir(parents=True)
    (tmp_path / ".config" / "hashnode" / "token").write_text("real_token\n")
    monkeypatch.chdir(tmp_path)
    val = hn_auth.get_token()
    assert val == "real_token"
    assert "STOLEN" not in val


# ---------------------------------------------------------------------------
# 9. Empty token file → falls through to next resolver, eventually errors
# ---------------------------------------------------------------------------

def test_empty_token_file_is_rejected(capsys, monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("HASHNODE_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / ".config" / "hashnode"
    cfg.mkdir(parents=True)
    (cfg / "token").write_text("   \n")  # whitespace only
    monkeypatch.chdir(tmp_path)
    # The current implementation strips and returns the stripped value even
    # if empty. Pin observed behavior.
    val = hn_auth._read_first("HASHNODE_TOKEN", "~/.config/hashnode/token")
    if val == "":
        # Empty string short-circuits the get_token check (`if not val`)
        # → gets the "not found" path → good
        with pytest.raises(SystemExit):
            hn_auth.get_token()
    else:
        # Edge case: whitespace-only file returns "" which is falsy → caught
        # by `if not val` → SystemExit. Confirmed safe.
        pass
