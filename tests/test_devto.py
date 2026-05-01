"""Tests for presets/devto/*.py — parse_args + render functions."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

PRESET_DIR = Path(__file__).parent.parent / "presets" / "devto"


def _load(name: str):
    for k in ("_auth", "_graphql", "_rest"):
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
    tag, n = browse.parse_args("ai:7")
    assert tag == "ai" and n == 7


def test_browse_render_empty() -> None:
    assert browse.render("ai", []) == "(no articles on tag ai)"


def test_browse_render_formats() -> None:
    out = browse.render("ai", [{
        "title": "P", "url": "https://x.io/p",
        "published_at": "2026-05-01T00:00:00Z",
        "user": {"username": "bob"},
        "public_reactions_count": 2, "comments_count": 1,
    }])
    assert "@bob" in out and "P" in out


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
