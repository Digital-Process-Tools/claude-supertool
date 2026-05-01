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
    for k in ("_auth", "_graphql", "_rest"):
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
    slug, post_id = read_op.parse_arg("https://example.hashnode.dev/my-slug")
    assert slug == "my-slug" and post_id is None


def test_read_parse_arg_slug() -> None:
    slug, post_id = read_op.parse_arg("my-slug")
    assert slug == "my-slug" and post_id is None


def test_read_parse_arg_object_id() -> None:
    slug, post_id = read_op.parse_arg("abc123def456")
    assert slug is None and post_id == "abc123def456"


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
    tag, n = browse_op.parse_args("ai")
    assert tag == "ai" and n == 10


def test_browse_parse_args_with_limit() -> None:
    tag, n = browse_op.parse_args("ai:5")
    assert tag == "ai" and n == 5


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
    }])
    assert "@alice" in out and "Post" in out


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
