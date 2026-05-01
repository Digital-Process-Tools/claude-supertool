"""Tests for presets/hashnode/*.py — parse_args + render functions.

Network calls are not exercised here (those are integration tests requiring
real tokens). Tests cover the pure-function surface of each op.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PRESET_DIR = Path(__file__).parent.parent / "presets" / "hashnode"


def _load(name: str):
    # Clear cached helper modules from any prior preset (devto) and prepend our dir.
    for k in ("_auth", "_graphql", "_rest", "_me", "_outbound", "_session"):
        sys.modules.pop(k, None)
    sys.path[:] = [p for p in sys.path if "presets/devto" not in p]
    if str(PRESET_DIR) not in sys.path:
        sys.path.insert(0, str(PRESET_DIR))
    spec = importlib.util.spec_from_file_location(f"hn_{name}", PRESET_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


publish_op = _load("publish")
list_op = _load("list")
read_op = _load("read")
browse_op = _load("browse")
comments_op = _load("comments")
comment_op = _load("comment")
reply_op = _load("reply")
search_op = _load("search")
status_since_op = _load("status_since")
outbound_op = _load("_outbound")


# publish ------------------------------------------------------------------

def test_publish_parse_args_minimal(tmp_path: Path) -> None:
    md = tmp_path / "p.md"
    md.write_text("body")
    parsed = publish_op.parse_args(f"T|{md}|https://x.io")
    assert parsed["title"] == "T"
    assert parsed["markdown"] == "body"
    assert parsed["canonical"] == "https://x.io"
    assert parsed["tags"] == []
    assert parsed["cover"] == ""


def test_publish_parse_args_with_tags(tmp_path: Path) -> None:
    md = tmp_path / "p.md"
    md.write_text("body")
    parsed = publish_op.parse_args(f"T|{md}|https://x.io|ai,programming")
    assert parsed["tags"] == [
        {"slug": "ai", "name": "Ai"},
        {"slug": "programming", "name": "Programming"},
    ]


def test_publish_parse_args_defaults_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    md = tmp_path / "p.md"
    md.write_text("body")
    monkeypatch.setenv("SUPERTOOL_DEFAULT_TAGS", "devops")
    monkeypatch.setenv("SUPERTOOL_DEFAULT_COVER", "https://default.png")
    parsed = publish_op.parse_args(f"T|{md}|https://x.io")
    assert parsed["tags"] == [{"slug": "devops", "name": "Devops"}]
    assert parsed["cover"] == "https://default.png"


def test_publish_parse_args_caller_overrides_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    md = tmp_path / "p.md"
    md.write_text("body")
    monkeypatch.setenv("SUPERTOOL_DEFAULT_TAGS", "devops")
    parsed = publish_op.parse_args(f"T|{md}|https://x.io|ai")
    assert parsed["tags"] == [{"slug": "ai", "name": "Ai"}]


def test_publish_parse_args_md_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        publish_op.parse_args(f"T|{tmp_path / 'no.md'}|https://x.io")
    assert "not found" in capsys.readouterr().err


def test_publish_build_input_minimal() -> None:
    parsed: dict = {"title": "T", "markdown": "body", "canonical": "https://x.io", "tags": [], "cover": ""}
    inp = publish_op.build_input(parsed, "pub-id-123")
    assert inp == {
        "publicationId": "pub-id-123",
        "title": "T",
        "contentMarkdown": "body",
        "originalArticleURL": "https://x.io",
    }


def test_publish_build_input_full() -> None:
    parsed: dict = {
        "title": "T", "markdown": "body", "canonical": "https://x.io",
        "tags": [{"slug": "ai", "name": "Ai"}],
        "cover": "https://x.io/og.png",
    }
    inp = publish_op.build_input(parsed, "pub-id-123")
    assert inp["tags"] == [{"slug": "ai", "name": "Ai"}]
    assert inp["coverImageOptions"] == {"coverImageURL": "https://x.io/og.png"}


# list ---------------------------------------------------------------------

def test_list_parse_args_default_no_arg() -> None:
    user, n = list_op.parse_args("")
    assert user is None and n == 10


def test_list_parse_args_numeric_only_means_own_with_limit() -> None:
    user, n = list_op.parse_args("5")
    assert user is None and n == 5


def test_list_parse_args_user() -> None:
    user, n = list_op.parse_args("alice")
    assert user == "alice" and n == 10


def test_list_parse_args_user_with_limit() -> None:
    user, n = list_op.parse_args("alice:25")
    assert user == "alice" and n == 25


def test_list_parse_args_default_limit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERTOOL_DEFAULT_LIMIT", "3")
    user, n = list_op.parse_args("")
    assert n == 3


def test_list_render_empty() -> None:
    assert list_op.render_posts([]) == "(no posts)"


def test_list_render_formats_post() -> None:
    out = list_op.render_posts([{
        "title": "Hello",
        "url": "https://x.io/hello",
        "publishedAt": "2026-05-01T10:00:00Z",
        "reactionCount": 3,
        "responseCount": 1,
    }])
    assert "Hello" in out and "2026-05-01" in out and "3 reactions" in out and "1 comments" in out


# read ---------------------------------------------------------------------

def test_read_parse_arg_url() -> None:
    slug, post_id, host = read_op.parse_arg("https://example.hashnode.dev/my-slug")
    assert slug == "my-slug" and post_id is None and host == "example.hashnode.dev"


def test_read_parse_arg_slug() -> None:
    slug, post_id, host = read_op.parse_arg("my-slug")
    assert slug == "my-slug" and post_id is None and host is None


def test_read_parse_arg_object_id() -> None:
    slug, post_id, host = read_op.parse_arg("abc123def456")
    assert slug is None and post_id == "abc123def456" and host is None


def test_read_parse_arg_empty_exits(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        read_op.parse_arg("")
    assert "ERROR" in capsys.readouterr().err


def test_read_render_with_comments_and_tags() -> None:
    out = read_op.render({
        "id": "abc",
        "title": "T",
        "url": "https://x.io/t",
        "publishedAt": "2026-05-01T00:00:00Z",
        "reactionCount": 5,
        "responseCount": 2,
        "author": {"name": "Max", "username": "max"},
        "tags": [{"slug": "ai", "name": "AI"}, {"slug": "identity", "name": "Identity"}],
        "content": {"markdown": "# Body\n\ntext"},
        "comments": {"edges": [
            {"node": {"id": "comm-7", "dateAdded": "2026-05-01T00:00:00Z",
                       "author": {"username": "alice"},
                       "content": {"markdown": "Nice post"}}},
        ]},
    }, inline_n=5)
    assert "TITLE:    T" in out and "@max" in out and "# Body" in out
    assert "ai, identity" in out
    assert "@alice: Nice post" in out
    assert "[id=comm-7]" in out
    assert "top 1 comments" in out
    assert "hashnode_react:abc" in out
    assert "hashnode_comment:abc|MSG" in out


def test_read_render_no_comments() -> None:
    out = read_op.render({
        "id": "abc", "title": "T", "url": "https://x.io",
        "publishedAt": "2026-05-01T00:00:00Z",
        "reactionCount": 0, "responseCount": 0,
        "author": {"username": "max"},
        "tags": [],
        "content": {"markdown": "body"},
        "comments": {"edges": []},
    }, inline_n=5)
    assert "0 comments" in out and "(none)" in out


# browse -------------------------------------------------------------------

def test_browse_parse_args() -> None:
    tag, n, sort = browse_op.parse_args("ai")
    assert tag == "ai" and n == 10 and sort == "recent"


def test_browse_parse_args_with_limit() -> None:
    tag, n, sort = browse_op.parse_args("ai:5")
    assert tag == "ai" and n == 5 and sort == "recent"


def test_browse_parse_args_top() -> None:
    tag, n, sort = browse_op.parse_args("ai:5:top")
    assert tag == "ai" and n == 5 and sort == "popular"


def test_browse_parse_args_sort_only() -> None:
    tag, n, sort = browse_op.parse_args("ai:popular")
    assert tag == "ai" and sort == "popular"


def test_browse_parse_args_unknown_token(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        browse_op.parse_args("ai:bogus")
    assert "unknown sort/limit" in capsys.readouterr().err


def test_browse_parse_args_empty_exits(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        browse_op.parse_args("")
    assert "ERROR" in capsys.readouterr().err


def test_browse_render_empty() -> None:
    assert browse_op.render("ai", []) == "(no posts on tag ai)"


def test_browse_render_formats() -> None:
    out = browse_op.render("ai", [{
        "title": "Post",
        "url": "https://x.io/p",
        "publishedAt": "2026-05-01T00:00:00Z",
        "reactionCount": 1,
        "responseCount": 0,
        "author": {"username": "alice"},
    }], sort="popular")
    assert "@alice" in out and "Post" in out and "sort=popular" in out


# comments -----------------------------------------------------------------

def test_comments_parse_args_url() -> None:
    slug, n = comments_op.parse_args("https://x.hashnode.dev/my-slug")
    assert slug == "my-slug" and n == 20


def test_comments_parse_args_slug_with_limit() -> None:
    slug, n = comments_op.parse_args("my-slug:5")
    assert slug == "my-slug" and n == 5


def test_comments_render_no_comments() -> None:
    out = comments_op.render("my-slug", {"title": "T", "comments": {"edges": []}})
    assert "0 comments" in out


def test_comments_render_with_comments() -> None:
    post = {
        "title": "T",
        "comments": {"edges": [
            {"node": {"dateAdded": "2026-05-01T00:00:00Z",
                       "author": {"username": "alice"},
                       "content": {"markdown": "Nice!"}}},
        ]},
    }
    out = comments_op.render("my-slug", post)
    assert "@alice" in out and "Nice!" in out


def test_comments_render_post_not_found() -> None:
    out = comments_op.render("missing", None)
    assert "not found" in out


# comment ------------------------------------------------------------------

def test_comment_parse_args_ok() -> None:
    post, msg = comment_op.parse_args("abc123|Hello world")
    assert post == "abc123" and msg == "Hello world"


def test_comment_parse_args_missing_msg(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        comment_op.parse_args("abc123")
    assert "ERROR" in capsys.readouterr().err


def test_comment_parse_args_empty_msg(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        comment_op.parse_args("abc123|   ")
    assert "ERROR" in capsys.readouterr().err


# reply --------------------------------------------------------------------

def test_reply_parse_args_ok() -> None:
    cid, msg = reply_op.parse_args("comm-7|Hello")
    assert cid == "comm-7" and msg == "Hello"


def test_reply_parse_args_missing_msg(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        reply_op.parse_args("comm-7")
    assert "ERROR" in capsys.readouterr().err


# search -------------------------------------------------------------------

def test_search_parse_args_default_limit() -> None:
    q, n = search_op.parse_args("burnout")
    assert q == "burnout" and n == 10


def test_search_parse_args_with_limit() -> None:
    q, n = search_op.parse_args("burnout:5")
    assert q == "burnout" and n == 5


def test_search_parse_args_keeps_colons_in_query() -> None:
    q, n = search_op.parse_args("a:b:c:3")
    assert q == "a:b:c" and n == 3


def test_search_render_empty() -> None:
    assert "no results" in search_op.render("burnout", [])


def test_search_render_formats() -> None:
    out = search_op.render("burnout", [{
        "id": "x", "title": "T", "url": "https://x.io",
        "publishedAt": "2026-05-01T00:00:00Z",
        "author": {"username": "max"},
        "reactionCount": 1, "responseCount": 0,
    }])
    assert "T" in out and "@max" in out


# status_since -------------------------------------------------------------

def test_status_since_filter_recent() -> None:
    post = {"comments": {"edges": [
        {"node": {"id": "1", "dateAdded": "2026-05-01T00:00:00Z"}},
        {"node": {"id": "2", "dateAdded": "2026-04-01T00:00:00Z"}},
    ]}}
    out = status_since_op.filter_recent(post, since="2026-04-15T00:00:00Z", max_per_post=10)
    assert len(out) == 1 and out[0]["id"] == "1"


def test_status_since_render_with_new_comments() -> None:
    pub = {
        "title": "max",
        "followersCount": 42,
        "posts": {"edges": [
            {"node": {
                "id": "p1", "title": "Burn", "url": "https://x.io/burn",
                "reactionCount": 5, "responseCount": 1,
                "comments": {"edges": [
                    {"node": {"id": "c1", "dateAdded": "2026-05-01T00:00:00Z",
                              "author": {"username": "alice"},
                              "content": {"markdown": "Nice"}}},
                ]},
            }},
        ]},
    }
    out = status_since_op.render(pub, since="2026-04-30T00:00:00Z", now="2026-05-01T12:00:00Z")
    assert "FOLLOWERS: 42" in out
    assert "NEW COMMENTS (1)" in out
    assert "@alice" in out
    assert "hashnode_reply:c1" in out
    assert "TOP POSTS" in out
    assert "--- NEXT ---" in out


def test_status_since_render_no_new() -> None:
    pub = {"title": "max", "followersCount": 0, "posts": {"edges": []}}
    out = status_since_op.render(pub, since="2026-04-30T00:00:00Z", now="2026-05-01T12:00:00Z")
    assert "(none)" in out


def test_status_since_resolve_since_uses_arg() -> None:
    assert status_since_op.resolve_since("2026-01-01T00:00:00Z") == "2026-01-01T00:00:00Z"


def test_status_since_filter_excludes_self() -> None:
    post = {"comments": {"edges": [
        {"node": {"id": "c1", "dateAdded": "2026-05-01T00:00:00Z",
                   "author": {"username": "max-ai-dev"}}},
        {"node": {"id": "c2", "dateAdded": "2026-05-01T00:00:00Z",
                   "author": {"username": "alice"}}},
    ]}}
    out = status_since_op.filter_recent(post, since="2026-04-30T00:00:00Z",
                                         max_per_post=10, me="max-ai-dev")
    assert len(out) == 1 and out[0]["id"] == "c2"


def test_status_since_count_my_recent() -> None:
    post = {"comments": {"edges": [
        {"node": {"dateAdded": "2026-05-01T00:00:00Z",
                   "author": {"username": "max-ai-dev"}}},
        {"node": {"dateAdded": "2026-05-01T00:00:00Z",
                   "author": {"username": "max-ai-dev"}}},
        {"node": {"dateAdded": "2026-04-01T00:00:00Z",  # too old
                   "author": {"username": "max-ai-dev"}}},
        {"node": {"dateAdded": "2026-05-01T00:00:00Z",
                   "author": {"username": "alice"}}},
    ]}}
    assert status_since_op.count_my_recent(post, "2026-04-30T00:00:00Z", "max-ai-dev") == 2


def test_status_since_render_includes_my_engagement() -> None:
    pub = {"title": "max", "followersCount": 0, "posts": {"edges": []}}
    out = status_since_op.render(pub, since="2026-04-30T00:00:00Z",
                                  now="2026-05-01T12:00:00Z", me="max-ai-dev")
    assert "MY ENGAGEMENT: 0 comments by you" in out


# read own engagement -----------------------------------------------------

def test_read_own_engagement() -> None:
    post = {"comments": {"edges": [
        {"node": {"id": "c1", "dateAdded": "2026-04-29T00:00:00Z",
                   "author": {"username": "max-ai-dev"}}},
        {"node": {"id": "c2", "dateAdded": "2026-04-30T00:00:00Z",
                   "author": {"username": "max-ai-dev"}}},
        {"node": {"id": "c3", "dateAdded": "2026-04-30T00:00:00Z",
                   "author": {"username": "alice"}}},
    ]}}
    n, ids, last = read_op.own_engagement(post, "max-ai-dev")
    assert n == 2 and ids == ["c1", "c2"] and last == "2026-04-30"


def test_read_render_shows_you_line() -> None:
    out = read_op.render({
        "id": "abc", "title": "T", "url": "https://x.io",
        "publishedAt": "2026-05-01T00:00:00Z",
        "reactionCount": 0, "responseCount": 0,
        "author": {"username": "other"},
        "tags": [],
        "content": {"markdown": "body"},
        "comments": {"edges": [
            {"node": {"id": "c1", "dateAdded": "2026-04-29T00:00:00Z",
                       "author": {"username": "max-ai-dev"},
                       "content": {"markdown": "Yo"}}},
        ]},
    }, inline_n=5, me="max-ai-dev")
    assert "YOU:" in out and "already commented 1×" in out and "c1" in out


# outbound + cross-post replies ------------------------------------------

def test_outbound_append_and_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    track = tmp_path / "outbound.jsonl"
    monkeypatch.setattr(outbound_op, "TRACK_FILE", track)
    outbound_op.append({"comment_id": "c1", "post_id": "p1", "parent_id": None,
                         "posted_at": "2026-05-01T00:00:00Z"})
    records = outbound_op.read()
    assert len(records) == 1 and records[0]["comment_id"] == "c1"


def test_outbound_unique_post_ids() -> None:
    pids = outbound_op.unique_post_ids([
        {"post_id": "a"}, {"post_id": "b"}, {"post_id": "a"},
        {"post_id": None}, {"comment_id": "x"},
    ])
    assert pids == ["a", "b"]


def test_outbound_my_comment_ids() -> None:
    assert outbound_op.my_comment_ids([{"comment_id": "x"}, {"foo": 1}]) == {"x"}


def test_outbound_replied_parent_ids() -> None:
    records = [
        {"comment_id": "c1", "parent_id": "pa"},
        {"comment_id": "c2", "parent_id": "pb"},
        {"comment_id": "c3", "parent_id": None},  # top-level, not a reply
        {"comment_id": "c4"},  # missing
    ]
    assert outbound_op.replied_parent_ids(records) == {"pa", "pb"}


def test_status_since_render_marks_already_replied() -> None:
    pub = {
        "title": "max", "followersCount": 0,
        "posts": {"edges": [
            {"node": {
                "id": "p1", "title": "T", "url": "https://x.io/t",
                "reactionCount": 0, "responseCount": 0,
                "comments": {"edges": [
                    {"node": {"id": "c-old-replied", "dateAdded": "2026-05-01T00:00:00Z",
                              "author": {"username": "alice"}, "content": {"markdown": "Q"}}},
                    {"node": {"id": "c-new", "dateAdded": "2026-05-01T00:00:00Z",
                              "author": {"username": "bob"}, "content": {"markdown": "Q2"}}},
                ]},
            }},
        ]},
    }
    out = status_since_op.render(pub, since="2026-04-30T00:00:00Z",
                                  now="2026-05-01T12:00:00Z",
                                  replied_to={"c-old-replied"})
    assert "c-old-replied] on 'T' (https://x.io/t) (already replied)" in out
    assert "c-new] on 'T' (https://x.io/t)" in out
    assert "(already replied)" in out
    # NEXT line still printed for the unreplied comment, suppressed for the replied
    assert "hashnode_reply:c-new" in out
    assert "hashnode_reply:c-old-replied" not in out


def test_status_since_find_replies_on_post() -> None:
    post = {"comments": {"edges": [
        {"node": {"id": "mine",
                   "replies": {"edges": [
                       {"node": {"id": "r1", "dateAdded": "2026-05-01T00:00:00Z",
                                  "author": {"username": "alice"},
                                  "content": {"markdown": "Hi"}}},
                       {"node": {"id": "r-old", "dateAdded": "2026-04-01T00:00:00Z",
                                  "author": {"username": "bob"}}},
                   ]}}},
        {"node": {"id": "not-mine",
                   "replies": {"edges": [
                       {"node": {"id": "r-other", "dateAdded": "2026-05-01T00:00:00Z"}},
                   ]}}},
    ]}}
    out = status_since_op.find_replies_on_post(post, my_ids={"mine"}, since="2026-04-30T00:00:00Z")
    assert len(out) == 1 and out[0]["id"] == "r1"


def test_status_since_render_replies_section_hashnode() -> None:
    pub = {"title": "max", "followersCount": 0, "posts": {"edges": []}}
    p = {"title": "Outside post", "url": "https://other.dev/post"}
    r = {"id": "r1", "dateAdded": "2026-05-01T00:00:00Z",
         "author": {"username": "alice"}, "content": {"markdown": "Hello"}}
    out = status_since_op.render(pub, since="2026-04-30T00:00:00Z",
                                  now="2026-05-01T12:00:00Z", me="max-ai-dev",
                                  replies_to_me=[(p, r)])
    assert "REPLIES TO YOUR COMMENTS (1)" in out
    assert "@alice" in out
    assert "hashnode_reply:r1" in out


def test_read_render_no_you_line_when_not_engaged() -> None:
    out = read_op.render({
        "id": "abc", "title": "T", "url": "https://x.io",
        "publishedAt": "2026-05-01T00:00:00Z",
        "reactionCount": 0, "responseCount": 0,
        "author": {"username": "other"},
        "tags": [],
        "content": {"markdown": "body"},
        "comments": {"edges": []},
    }, inline_n=5, me="max-ai-dev")
    assert "YOU:" not in out
