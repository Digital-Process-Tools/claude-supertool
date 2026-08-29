"""Integration half of #2042 for the four remaining publish ops
(`bluesky_publish`, `devto_comment`, `hashnode_comment`, `hashnode_reply`) --
proves the shared `apply_disclosure` helper actually reaches the body each
op sends, not just that the helper works in isolation
(`tests/test_publish_disclosure_2042.py`).

Conftest suppresses the marker suite-wide
(`SUPERTOOL_NO_PUBLISH_DISCLOSURE=1`); `real_defaults` below undoes that so
the on-by-default wiring is exercised for real, the same pattern
`tests/test_slack_disclosure_2042.py` uses.
"""
from __future__ import annotations

import pytest

from _netblock import block_outbound
from _preset_loader import load_preset_module

bluesky_publish = load_preset_module("bluesky", "publish", "bskydisc_")
devto_comment = load_preset_module("devto", "comment", "dtdisc_")
hashnode_comment = load_preset_module("hashnode", "comment", "hndisc_")
hashnode_reply = load_preset_module("hashnode", "reply", "hnrdisc_")


@pytest.fixture(autouse=True)
def _no_outbound(monkeypatch: pytest.MonkeyPatch) -> None:
    block_outbound(monkeypatch)


@pytest.fixture
def real_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPERTOOL_NO_PUBLISH_DISCLOSURE", raising=False)
    monkeypatch.chdir(tmp_path)
    import _publish_safety
    if hasattr(_publish_safety, "_CACHED_CONFIG"):
        delattr(_publish_safety, "_CACHED_CONFIG")
    yield
    if hasattr(_publish_safety, "_CACHED_CONFIG"):
        delattr(_publish_safety, "_CACHED_CONFIG")


def test_bluesky_publish_carries_the_marker_by_default(real_defaults, monkeypatch) -> None:
    captured: dict = {}

    def fake_xrpc(nsid, session, method="GET", params=None, body=None, **kw):
        captured["record"] = body["record"]
        return {"uri": "at://did:1/app.bsky.feed.post/x", "cid": "c1"}

    monkeypatch.setattr(bluesky_publish, "get_handle", lambda: "me.bsky.social")
    monkeypatch.setattr(bluesky_publish, "get_app_password", lambda: "pw")
    monkeypatch.setattr(bluesky_publish, "get_session", lambda h, p: {"did": "did:1"})
    monkeypatch.setattr(bluesky_publish, "xrpc", fake_xrpc)

    bluesky_publish.main("Deploy finished||force")

    assert captured["record"]["text"] != "Deploy finished"
    assert "Deploy finished" in captured["record"]["text"]


def test_bluesky_publish_drops_the_marker_rather_than_exceed_300_chars(
    real_defaults, monkeypatch,
) -> None:
    captured: dict = {}

    def fake_xrpc(nsid, session, method="GET", params=None, body=None, **kw):
        captured["record"] = body["record"]
        return {"uri": "at://did:1/app.bsky.feed.post/x", "cid": "c1"}

    monkeypatch.setattr(bluesky_publish, "get_handle", lambda: "me.bsky.social")
    monkeypatch.setattr(bluesky_publish, "get_app_password", lambda: "pw")
    monkeypatch.setattr(bluesky_publish, "get_session", lambda h, p: {"did": "did:1"})
    monkeypatch.setattr(bluesky_publish, "xrpc", fake_xrpc)

    body_at_limit = "x" * 300
    bluesky_publish.main(f"{body_at_limit}||force")

    assert captured["record"]["text"] == body_at_limit


def test_devto_comment_carries_the_marker_by_default(real_defaults, monkeypatch, capsys) -> None:
    captured: dict = {}

    def fake_web_post(path, cookie, csrf, body):
        captured["body"] = body
        return ('{"id": 42}', 200)

    monkeypatch.setattr(devto_comment, "resolve_article_id", lambda raw: 999)
    monkeypatch.setattr(devto_comment, "get_session_cookie", lambda: "cookie-val")
    monkeypatch.setattr(devto_comment, "fetch_csrf_token", lambda c: "csrf-tok")
    monkeypatch.setattr(devto_comment, "web_post_json", fake_web_post)
    monkeypatch.setattr(devto_comment, "_print_post_confirmation", lambda aid, cid: None)

    devto_comment.main("999|Hello world||force")

    posted = captured["body"]["comment"]["body_markdown"]
    assert posted != "Hello world"
    assert "Hello world" in posted


def test_hashnode_comment_carries_the_marker_by_default(real_defaults, monkeypatch, capsys) -> None:
    captured: dict = {}

    def fake_gql(query, variables, token):
        captured["variables"] = variables
        return {"addComment": {"comment": {"id": "new-c", "dateAdded": "2026-05-01T00:00:00Z"}}}

    monkeypatch.setattr(hashnode_comment, "get_token", lambda: "tok")
    monkeypatch.setattr(hashnode_comment, "resolve_post_id", lambda t, r: "post-id")
    monkeypatch.setattr(hashnode_comment, "gql", fake_gql)
    monkeypatch.setattr(hashnode_comment, "track_append", lambda x: None)

    hashnode_comment.main("abc|Hello|force")

    posted = captured["variables"]["input"]["contentMarkdown"]
    assert posted != "Hello"
    assert "Hello" in posted


def test_hashnode_reply_carries_the_marker_by_default(real_defaults, monkeypatch) -> None:
    captured: dict = {}

    def fake_gql(query, variables, token):
        if "input" in variables:
            captured["variables"] = variables
            return {"addReply": {"reply": {"id": "r1", "dateAdded": "2026-05-01T00:00:00Z"}}}
        return {"comment": {"post": {"id": "post-id"}}}

    monkeypatch.setattr(hashnode_reply, "get_token", lambda: "tok")
    monkeypatch.setattr(hashnode_reply, "gql", fake_gql)
    monkeypatch.setattr(hashnode_reply, "track_append", lambda x: None)

    hashnode_reply.main("comm-7|Hello")

    posted = captured["variables"]["input"]["contentMarkdown"]
    assert posted != "Hello"
    assert "Hello" in posted
