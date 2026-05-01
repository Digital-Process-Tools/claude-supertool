"""Tests for presets/bluesky/*.py — pure-function surface only.

Network calls (createSession, xrpc) are not exercised here. Tests
focus on argument parsing and output rendering.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PRESET_DIR = Path(__file__).parent.parent / "presets" / "bluesky"


def _load(name: str):
    for k in ("_auth", "_atproto", "_me", "_outbound", "_session", "_rest", "_graphql"):
        sys.modules.pop(k, None)
    sys.path[:] = [p for p in sys.path
                    if "presets/devto" not in p and "presets/hashnode" not in p]
    if str(PRESET_DIR) not in sys.path:
        sys.path.insert(0, str(PRESET_DIR))
    spec = importlib.util.spec_from_file_location(f"bsky_{name}", PRESET_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


publish = _load("publish")
list_op = _load("list")
search_op = _load("search")
read_op = _load("read")
status_since_op = _load("status_since")


# publish ---------------------------------------------------------------

def test_publish_parse_args_inline_text() -> None:
    body, reply = publish.parse_args("Hello, real humans.")
    assert body == "Hello, real humans." and reply is None


def test_publish_parse_args_with_reply(tmp_path: Path) -> None:
    body, reply = publish.parse_args("Reply text|at://did:plc:abc/app.bsky.feed.post/3kxyz")
    assert body == "Reply text"
    assert reply == "at://did:plc:abc/app.bsky.feed.post/3kxyz"


def test_publish_parse_args_text_file(tmp_path: Path) -> None:
    md = tmp_path / "post.txt"
    md.write_text("From a file")
    body, _ = publish.parse_args(str(md))
    assert body == "From a file"


def test_publish_parse_args_too_long(capsys: pytest.CaptureFixture[str]) -> None:
    long_text = "x" * 301
    with pytest.raises(SystemExit):
        publish.parse_args(long_text)
    err = capsys.readouterr().err
    assert "max" in err and "301" in err


def test_publish_parse_args_empty(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        publish.parse_args("")
    assert "ERROR" in capsys.readouterr().err


# list ------------------------------------------------------------------

def test_list_parse_args_default() -> None:
    actor, n = list_op.parse_args("")
    assert actor is None and n == 10


def test_list_parse_args_handle_with_limit() -> None:
    actor, n = list_op.parse_args("alice.bsky.social|5")
    assert actor == "alice.bsky.social" and n == 5


def test_list_render_empty() -> None:
    assert list_op.render([]) == "(no posts)"


def test_list_render_formats() -> None:
    out = list_op.render([
        {"post": {"author": {"handle": "alice.bsky.social"},
                   "record": {"text": "Hi", "createdAt": "2026-05-01T00:00:00Z"},
                   "replyCount": 1, "likeCount": 3}},
    ])
    assert "@alice.bsky.social" in out and "Hi" in out and "3 likes" in out


# search ----------------------------------------------------------------

def test_search_parse_args_with_limit() -> None:
    q, n = search_op.parse_args("claude-code|7")
    assert q == "claude-code" and n == 7


def test_search_parse_args_default() -> None:
    q, n = search_op.parse_args("identity")
    assert q == "identity" and n == 10


def test_search_render_empty() -> None:
    assert "no results" in search_op.render("xx", [])


# read ------------------------------------------------------------------

def test_read_render_with_replies() -> None:
    thread = {
        "post": {
            "uri": "at://did:plc:abc/app.bsky.feed.post/3kxyz",
            "author": {"handle": "alice.bsky.social", "displayName": "Alice"},
            "record": {"text": "Body here", "createdAt": "2026-05-01T00:00:00Z"},
            "likeCount": 5, "replyCount": 1, "repostCount": 0,
        },
        "replies": [
            {"post": {"uri": "at://did:plc:abc/app.bsky.feed.post/3kxyz-r1",
                       "author": {"handle": "bob.bsky.social"},
                       "record": {"text": "Nice"}}},
        ],
    }
    out = read_op.render(thread, inline_n=5)
    assert "@alice.bsky.social" in out and "Body here" in out
    assert "@bob.bsky.social" in out and "Nice" in out
    assert "bluesky_like:" in out
    assert "bluesky_publish:" in out


def test_read_render_no_replies() -> None:
    thread = {
        "post": {
            "uri": "at://x/x/x",
            "author": {"handle": "x.bsky.social"},
            "record": {"text": "x", "createdAt": "2026-05-01T00:00:00Z"},
            "likeCount": 0, "replyCount": 0, "repostCount": 0,
        },
        "replies": [],
    }
    out = read_op.render(thread, inline_n=5)
    assert "0 replies" in out


# status_since ----------------------------------------------------------

def test_status_since_resolve_arg_wins() -> None:
    assert status_since_op.resolve_since("2026-01-01T00:00:00Z") == "2026-01-01T00:00:00Z"


def test_status_since_render_empty() -> None:
    out = status_since_op.render([], since="2026-04-30T00:00:00Z", now="2026-05-01T12:00:00Z")
    assert "(none)" in out


def test_status_since_render_with_kinds() -> None:
    notifs = [
        {"reason": "mention", "indexedAt": "2026-05-01T01:00:00Z",
         "uri": "at://x/y/z",
         "author": {"handle": "alice.bsky.social"},
         "record": {"text": "@max-ai-dev hey"}},
        {"reason": "follow", "indexedAt": "2026-05-01T02:00:00Z",
         "uri": "at://x/y/z2",
         "author": {"handle": "bob.bsky.social"},
         "record": {}},
    ]
    out = status_since_op.render(notifs, since="2026-04-30T00:00:00Z", now="2026-05-01T12:00:00Z")
    assert "MENTIONS (1)" in out
    assert "FOLLOWS (1)" in out
    assert "@alice.bsky.social" in out and "@bob.bsky.social" in out
    assert "bluesky_publish:" in out  # reply NEXT
    assert "bluesky_follow:" in out  # follow back NEXT
