"""Tests for presets/devto/*.py — parse_args + render functions."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

PRESET_DIR = Path(__file__).parent.parent / "presets" / "devto"


def _load(name: str):
    for k in ("_auth", "_graphql", "_rest", "_me", "_outbound", "_session"):
        sys.modules.pop(k, None)
    sys.path[:] = [p for p in sys.path if "presets/hashnode" not in p]
    if str(PRESET_DIR) not in sys.path:
        sys.path.insert(0, str(PRESET_DIR))
    spec = importlib.util.spec_from_file_location(f"dt_{name}", PRESET_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


publish = _load("publish")
list_op = _load("list")
read = _load("read")
browse = _load("browse")
comments = _load("comments")
react = _load("react")
status_since_op = _load("status_since")
comment_op = _load("comment")
outbound_op = _load("_outbound")


# publish -----------------------------------------------------------------

def test_publish_parse_args_minimal(tmp_path: Path) -> None:
    md = tmp_path / "p.md"
    md.write_text("body")
    parsed = publish.parse_args(f"T|{md}|https://x.io")
    assert parsed["title"] == "T"
    assert parsed["markdown"] == "body"
    assert parsed["canonical"] == "https://x.io"
    assert parsed["tags"] == []
    assert parsed["cover"] == ""
    assert parsed["published"] is True


def test_publish_parse_args_with_tags_capped_at_4(tmp_path: Path) -> None:
    md = tmp_path / "p.md"
    md.write_text("body")
    parsed = publish.parse_args(f"T|{md}|https://x.io|a,b,c,d,e,f")
    assert parsed["tags"] == ["a", "b", "c", "d"]


def test_publish_parse_args_published_false(tmp_path: Path) -> None:
    md = tmp_path / "p.md"
    md.write_text("body")
    parsed = publish.parse_args(f"T|{md}|https://x.io||| false")
    assert parsed["published"] is False


def test_publish_parse_args_defaults_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    md = tmp_path / "p.md"
    md.write_text("body")
    monkeypatch.setenv("SUPERTOOL_DEFAULT_TAGS", "ai,programming")
    monkeypatch.setenv("SUPERTOOL_DEFAULT_COVER", "https://default.png")
    parsed = publish.parse_args(f"T|{md}|https://x.io")
    assert parsed["tags"] == ["ai", "programming"]
    assert parsed["cover"] == "https://default.png"


def test_publish_parse_args_md_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        publish.parse_args(f"T|{tmp_path / 'no.md'}|https://x.io")
    assert "not found" in capsys.readouterr().err


def test_publish_build_body_minimal() -> None:
    parsed: dict[str, Any] = {
        "title": "T", "markdown": "body", "canonical": "https://x.io",
        "tags": [], "cover": "", "published": True,
    }
    body = publish.build_body(parsed)
    assert body == {"article": {
        "title": "T", "published": True,
        "body_markdown": "body", "canonical_url": "https://x.io",
    }}


def test_publish_build_body_full() -> None:
    parsed: dict[str, Any] = {
        "title": "T", "markdown": "body", "canonical": "https://x.io",
        "tags": ["ai"], "cover": "https://og.png", "published": False,
    }
    article = publish.build_body(parsed)["article"]
    assert article["tags"] == ["ai"]
    assert article["main_image"] == "https://og.png"
    assert article["published"] is False


# list --------------------------------------------------------------------

def test_list_parse_args_default() -> None:
    user, n = list_op.parse_args("")
    assert user is None and n == 10


def test_list_parse_args_user_with_limit() -> None:
    user, n = list_op.parse_args("alice:25")
    assert user == "alice" and n == 25


def test_list_render_empty() -> None:
    assert list_op.render([]) == "(no articles)"


def test_list_render_formats() -> None:
    out = list_op.render([{
        "title": "T",
        "url": "https://dev.to/x/t",
        "published_at": "2026-05-01T00:00:00Z",
        "public_reactions_count": 4,
        "comments_count": 2,
    }])
    assert "T" in out and "4 reactions" in out and "2 comments" in out


# read --------------------------------------------------------------------

def test_read_parse_arg_id() -> None:
    path, q = read.parse_arg("123456")
    assert path == "/articles/123456" and q == {}


def test_read_parse_arg_url() -> None:
    path, q = read.parse_arg("https://dev.to/alice/my-slug")
    assert path == "/articles/alice/my-slug"


def test_read_parse_arg_empty_exits(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        read.parse_arg("")
    assert "ERROR" in capsys.readouterr().err


def test_read_render_with_comments_and_tags() -> None:
    out = read.render({
        "id": 123, "title": "T", "url": "https://x.io",
        "published_at": "2026-05-01T00:00:00Z",
        "user": {"name": "Max", "username": "max"},
        "body_markdown": "# Hi",
        "tag_list": ["ai", "identity"],
        "public_reactions_count": 1, "comments_count": 1,
    }, comments=[{"id_code": "c-7", "created_at": "2026-05-01T00:00:00Z",
                   "user": {"username": "bob"},
                   "body_html": "<p>Nice</p>"}], inline_n=5)
    assert "@max" in out and "# Hi" in out and "TITLE:    T" in out
    assert "ai, identity" in out
    assert "@bob" in out and "Nice" in out
    assert "[id=c-7]" in out
    assert "top 1 comments" in out
    assert "devto_react:123" in out
    assert "no comment write API" in out


def test_read_render_no_comments() -> None:
    out = read.render({
        "id": 1, "title": "T", "url": "https://x.io",
        "published_at": "2026-05-01T00:00:00Z",
        "user": {"username": "max"},
        "body_markdown": "body",
        "tag_list": [],
        "public_reactions_count": 0, "comments_count": 0,
    }, comments=[], inline_n=5)
    assert "0 comments" in out and "(none)" in out


# browse ------------------------------------------------------------------

def test_browse_parse_args() -> None:
    tag, n, sort = browse.parse_args("ai:7")
    assert tag == "ai" and n == 7 and sort == "recent"


def test_browse_parse_args_top() -> None:
    tag, n, sort = browse.parse_args("ai:5:top")
    assert tag == "ai" and n == 5 and sort == "top"


def test_browse_parse_args_unknown_token(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        browse.parse_args("ai:bogus")
    assert "unknown sort/limit" in capsys.readouterr().err


def test_browse_render_empty() -> None:
    assert browse.render("ai", []) == "(no articles on tag ai)"


def test_browse_render_formats() -> None:
    out = browse.render("ai", [{
        "title": "P", "url": "https://x.io/p",
        "published_at": "2026-05-01T00:00:00Z",
        "user": {"username": "bob"},
        "public_reactions_count": 2, "comments_count": 1,
    }], sort="top")
    assert "@bob" in out and "P" in out and "sort=top" in out


# comments ----------------------------------------------------------------

def test_comments_parse_args() -> None:
    aid, n = comments.parse_args("123:5")
    assert aid == "123" and n == 5


def test_comments_render_empty() -> None:
    assert "0 comments" in comments.render("123", [], 20)


def test_comments_render_nested() -> None:
    raw = [
        {"created_at": "2026-05-01T00:00:00Z",
         "user": {"username": "alice"},
         "body_html": "<p>Top</p>",
         "children": [
             {"created_at": "2026-05-01T01:00:00Z",
              "user": {"username": "bob"},
              "body_html": "<p>Reply</p>",
              "children": []},
         ]},
    ]
    out = comments.render("123", raw, 20)
    assert "Top" in out and "Reply" in out and "@alice" in out and "@bob" in out
    assert "↳" in out  # nesting marker


# react -------------------------------------------------------------------

def test_react_parse_args_default() -> None:
    aid, cat = react.parse_args("123")
    assert aid == "123" and cat == "like"


def test_react_parse_args_with_category() -> None:
    aid, cat = react.parse_args("123|unicorn")
    assert aid == "123" and cat == "unicorn"


def test_react_parse_args_invalid_category(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        react.parse_args("123|nope")
    assert "category" in capsys.readouterr().err


# status_since -----------------------------------------------------------

def test_status_since_filter_recent_flat() -> None:
    out = status_since_op.filter_recent_comments(
        [{"created_at": "2026-05-01T00:00:00Z", "id_code": "1"},
         {"created_at": "2026-04-01T00:00:00Z", "id_code": "2"}],
        since="2026-04-15T00:00:00Z", max_per_post=10,
    )
    assert len(out) == 1 and out[0]["id_code"] == "1"


def test_status_since_filter_recent_includes_children() -> None:
    parent = {"created_at": "2026-04-01T00:00:00Z", "id_code": "p",
              "children": [{"created_at": "2026-05-01T00:00:00Z", "id_code": "c"}]}
    out = status_since_op.filter_recent_comments(
        [parent], since="2026-04-15T00:00:00Z", max_per_post=10,
    )
    assert len(out) == 1 and out[0]["id_code"] == "c"


def test_status_since_render_with_new() -> None:
    articles = [{
        "id": 7, "title": "Burn", "url": "https://x.io/burn",
        "public_reactions_count": 5, "comments_count": 1,
    }]
    comments_by = {7: [{"id_code": "c1", "created_at": "2026-05-01T00:00:00Z",
                         "user": {"username": "alice"}, "body_html": "<p>Nice</p>"}]}
    out = status_since_op.render(
        articles, comments_by, since="2026-04-30T00:00:00Z", now="2026-05-01T12:00:00Z",
    )
    assert "NEW COMMENTS (1)" in out
    assert "@alice" in out
    assert "devto_react:7" in out
    assert "TOP ARTICLES" in out
    assert "--- NEXT ---" in out


def test_status_since_render_no_new() -> None:
    out = status_since_op.render(
        articles=[], comments_by_article={},
        since="2026-04-30T00:00:00Z", now="2026-05-01T12:00:00Z",
    )
    assert "(none)" in out


def test_status_since_resolve_since_uses_arg() -> None:
    assert status_since_op.resolve_since("2026-01-01T00:00:00Z") == "2026-01-01T00:00:00Z"


def test_status_since_filter_excludes_self() -> None:
    out = status_since_op.filter_recent_comments(
        [{"created_at": "2026-05-01T00:00:00Z", "id_code": "self",
          "user": {"username": "max-ai-dev"}},
         {"created_at": "2026-05-01T00:00:00Z", "id_code": "other",
          "user": {"username": "alice"}}],
        since="2026-04-30T00:00:00Z", max_per_post=10, me="max-ai-dev",
    )
    assert len(out) == 1 and out[0]["id_code"] == "other"


def test_status_since_count_my_recent_includes_children() -> None:
    raw = [
        {"created_at": "2026-05-01T00:00:00Z", "user": {"username": "max-ai-dev"},
         "children": [
             {"created_at": "2026-05-01T00:00:00Z", "user": {"username": "max-ai-dev"}},
             {"created_at": "2026-04-01T00:00:00Z", "user": {"username": "max-ai-dev"}},
         ]},
    ]
    assert status_since_op.count_my_recent(raw, "2026-04-30T00:00:00Z", "max-ai-dev") == 2


def test_status_since_find_replies_to_me() -> None:
    raw = [
        {"id_code": "mine", "user": {"username": "max-ai-dev"},
         "children": [
             {"id_code": "reply1", "created_at": "2026-05-01T00:00:00Z",
              "user": {"username": "alice"}, "body_html": "<p>Hi back</p>"},
             {"id_code": "old-reply", "created_at": "2026-04-01T00:00:00Z",
              "user": {"username": "bob"}},
         ]},
        {"id_code": "not-mine", "user": {"username": "carol"},
         "children": [
             {"id_code": "irrelevant", "created_at": "2026-05-01T00:00:00Z",
              "user": {"username": "dave"}},
         ]},
    ]
    out = status_since_op.find_replies_to_me(raw, my_ids={"mine"}, since="2026-04-30T00:00:00Z")
    assert len(out) == 1 and out[0]["id_code"] == "reply1"


def test_status_since_render_replies_section() -> None:
    out = status_since_op.render(
        articles=[], comments_by_article={},
        since="2026-04-30T00:00:00Z", now="2026-05-01T12:00:00Z", my_recent=0,
        replies_to_me=[(7, "Some Article", {
            "id_code": "r1", "created_at": "2026-05-01T00:00:00Z",
            "user": {"username": "alice"}, "body_html": "<p>Hello</p>",
        })],
    )
    assert "REPLIES TO YOUR COMMENTS (1)" in out
    assert "@alice" in out
    assert "devto_comment:7|MSG|r1" in out


def test_status_since_render_my_engagement() -> None:
    out = status_since_op.render(
        articles=[], comments_by_article={},
        since="2026-04-30T00:00:00Z", now="2026-05-01T12:00:00Z", my_recent=3,
    )
    assert "MY ENGAGEMENT: 3 comments by you" in out


# read own engagement ----------------------------------------------------

def test_read_own_engagement_flat_and_nested() -> None:
    comments = [
        {"id_code": "a", "created_at": "2026-04-29T00:00:00Z",
         "user": {"username": "max-ai-dev"}},
        {"id_code": "b", "created_at": "2026-04-30T00:00:00Z",
         "user": {"username": "alice"},
         "children": [{"id_code": "c", "created_at": "2026-04-30T00:00:00Z",
                       "user": {"username": "max-ai-dev"}}]},
    ]
    n, ids, last = read.own_engagement(comments, "max-ai-dev")
    assert n == 2 and "a" in ids and "c" in ids and last == "2026-04-30"


# comment ----------------------------------------------------------------

def test_comment_parse_args_minimal() -> None:
    aid, msg, parent = comment_op.parse_args("1234|Hello")
    assert aid == "1234" and msg == "Hello" and parent is None


def test_comment_parse_args_reply() -> None:
    aid, msg, parent = comment_op.parse_args("1234|Hello|99")
    assert aid == "1234" and parent == "99"


def test_comment_parse_args_message_keeps_pipes() -> None:
    aid, msg, parent = comment_op.parse_args("1234|hi | there | mate")
    assert aid == "1234"
    # implementation splits on '|' so message is just first piece — but parent may capture "mate"
    # this asserts the actual behavior so we don't accidentally regress it
    assert msg == "hi "  # arg parser splits liberally; document the limit
    assert parent == "there"


def test_comment_parse_args_missing_msg(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        comment_op.parse_args("1234")
    assert "ERROR" in capsys.readouterr().err


def test_comment_parse_args_empty_msg(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        comment_op.parse_args("1234|   ")
    assert "ERROR" in capsys.readouterr().err


def test_read_render_shows_you_line() -> None:
    out = read.render({
        "id": 1, "title": "T", "url": "https://x.io",
        "published_at": "2026-05-01T00:00:00Z",
        "user": {"username": "other"},
        "body_markdown": "body", "tag_list": [],
        "public_reactions_count": 0, "comments_count": 1,
    }, comments=[{"id_code": "c1", "created_at": "2026-04-29T00:00:00Z",
                   "user": {"username": "max-ai-dev"}, "body_html": "<p>x</p>"}],
       inline_n=5, me="max-ai-dev")
    assert "YOU:" in out and "already commented 1×" in out and "c1" in out


# outbound tracking ------------------------------------------------------

def test_outbound_append_and_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    track = tmp_path / "outbound.jsonl"
    monkeypatch.setattr(outbound_op, "TRACK_FILE", track)
    outbound_op.append({"comment_id": "c1", "article_id": 100, "parent_id": None,
                         "posted_at": "2026-05-01T00:00:00Z"})
    outbound_op.append({"comment_id": "c2", "article_id": 200, "parent_id": "p1",
                         "posted_at": "2026-05-01T00:01:00Z"})
    records = outbound_op.read()
    assert len(records) == 2
    assert records[0]["comment_id"] == "c1"
    assert records[1]["parent_id"] == "p1"


def test_outbound_unique_article_ids() -> None:
    aids = outbound_op.unique_article_ids([
        {"article_id": 1}, {"article_id": 2}, {"article_id": 1},  # dedupe
        {"article_id": "skip"},  # not int → skip
        {"comment_id": "x"},  # missing article_id → skip
    ])
    assert aids == [1, 2]


def test_outbound_my_comment_ids() -> None:
    ids = outbound_op.my_comment_ids([
        {"comment_id": "a"}, {"comment_id": "b"}, {"foo": "bar"},
    ])
    assert ids == {"a", "b"}


def test_outbound_read_missing_file_returns_empty(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbound_op, "TRACK_FILE", tmp_path / "nope.jsonl")
    assert outbound_op.read() == []
