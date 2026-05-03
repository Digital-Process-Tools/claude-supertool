"""Tests for presets/devto/*.py — parse_args + render functions."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

PRESET_DIR = Path(__file__).parent.parent / "presets" / "devto"


def _load(name: str):
    for k in ("_auth", "_graphql", "_rest", "_me", "_outbound", "_session", "_resolve"):
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
resolve_op = _load("_resolve")


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


def test_publish_parse_args_force_flag(tmp_path: Path) -> None:
    """7th pipe field 'force' sets force=True."""
    md = tmp_path / "p.md"
    md.write_text("body")
    parsed = publish.parse_args(f"T|{md}|https://x.io||||force")
    assert parsed["force"] is True


def test_publish_parse_args_no_force_by_default(tmp_path: Path) -> None:
    """Omitting 7th field → force=False."""
    md = tmp_path / "p.md"
    md.write_text("body")
    parsed = publish.parse_args(f"T|{md}|https://x.io")
    assert parsed["force"] is False


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


def test_comments_render_marks_you() -> None:
    raw = [
        {"id_code": "abc", "created_at": "2026-05-01T00:00:00Z",
         "user": {"username": "max-ai-dev"}, "body_html": "<p>Mine</p>", "children": []},
        {"id_code": "xyz", "created_at": "2026-05-01T00:00:00Z",
         "user": {"username": "alice"}, "body_html": "<p>Theirs</p>", "children": []},
    ]
    out = comments.render("123", raw, 20, mine={"abc"})
    assert "[YOU] [id=abc]" in out
    assert "[YOU]" not in out.split("Theirs")[1] if "Theirs" in out else True
    assert "[id=xyz]" in out


# react -------------------------------------------------------------------

def test_react_parse_args_default() -> None:
    aid, cat, idempotent = react.parse_args("123")
    assert aid == "123" and cat == "like" and idempotent is True


def test_react_parse_args_with_category() -> None:
    aid, cat, idempotent = react.parse_args("123|unicorn")
    assert aid == "123" and cat == "unicorn" and idempotent is True


def test_react_parse_args_invalid_category(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        react.parse_args("123|nope")
    assert "category" in capsys.readouterr().err


def test_react_parse_args_idempotent_default() -> None:
    """No third field → idempotent=True (ensure-create semantics)."""
    aid, cat, idempotent = react.parse_args("123|like")
    assert idempotent is True


def test_react_parse_args_toggle_modifier() -> None:
    """|toggle as third field → idempotent=False (raw toggle, one POST)."""
    aid, cat, idempotent = react.parse_args("123|like|toggle")
    assert aid == "123" and cat == "like" and idempotent is False


def test_react_parse_args_toggle_modifier_default_category() -> None:
    """Toggle modifier works even when category is omitted (empty second field)."""
    aid, cat, idempotent = react.parse_args("123||toggle")
    assert cat == "like" and idempotent is False


def test_react_idempotent_create_no_double_post(monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    """result=create on first POST → no second POST needed."""
    posts: list[dict] = []

    def fake_web_post(path, cookie, csrf, body, **kw):
        posts.append(body)
        return ('{"result": "create"}', 200)

    monkeypatch.setattr(react, "get_session_cookie", lambda: "cookie-val")
    monkeypatch.setattr(react, "fetch_csrf_token", lambda c: "csrf-tok")
    monkeypatch.setattr(react, "web_post_json", fake_web_post)
    monkeypatch.setattr(react, "resolve_article_id", lambda raw: 42)

    react.main("42|like")

    assert len(posts) == 1
    out = capsys.readouterr()
    assert "result=create" in out.out
    assert "was_on=true" not in out.out


def test_react_idempotent_destroy_corrects(monkeypatch: pytest.MonkeyPatch,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    """result=destroy on first POST → second POST issued to restore; output shows was_on=true."""
    responses = iter(['{"result": "destroy"}', '{"result": "create"}'])
    posts: list[dict] = []

    def fake_web_post(path, cookie, csrf, body, **kw):
        posts.append(body)
        return (next(responses), 200)

    monkeypatch.setattr(react, "get_session_cookie", lambda: "cookie-val")
    monkeypatch.setattr(react, "fetch_csrf_token", lambda c: "csrf-tok")
    monkeypatch.setattr(react, "web_post_json", fake_web_post)
    monkeypatch.setattr(react, "resolve_article_id", lambda raw: 42)

    react.main("42|like")

    assert len(posts) == 2
    out = capsys.readouterr()
    assert "result=create" in out.out
    assert "was_on=true" in out.out
    assert "NOTE:" in out.err


def test_react_toggle_mode_no_correction(monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    """With |toggle, destroy result is NOT corrected — raw one-shot behaviour."""
    posts: list[dict] = []

    def fake_web_post(path, cookie, csrf, body, **kw):
        posts.append(body)
        return ('{"result": "destroy"}', 200)

    monkeypatch.setattr(react, "get_session_cookie", lambda: "cookie-val")
    monkeypatch.setattr(react, "fetch_csrf_token", lambda c: "csrf-tok")
    monkeypatch.setattr(react, "web_post_json", fake_web_post)
    monkeypatch.setattr(react, "resolve_article_id", lambda raw: 42)

    react.main("42|like|toggle")

    assert len(posts) == 1
    out = capsys.readouterr()
    assert "result=destroy" in out.out
    assert "was_on" not in out.out


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
    aid, msg, parent, force = comment_op.parse_args("1234|Hello")
    assert aid == "1234" and msg == "Hello" and parent is None and force is False


def test_comment_parse_args_reply() -> None:
    aid, msg, parent, force = comment_op.parse_args("1234|Hello|99")
    assert aid == "1234" and parent == "99" and force is False


def test_comment_parse_args_message_keeps_pipes() -> None:
    aid, msg, parent, force = comment_op.parse_args("1234|hi | there | mate")
    assert aid == "1234"
    # implementation splits on '|' so message is just first piece — but parent may capture "mate"
    # this asserts the actual behavior so we don't accidentally regress it
    assert msg == "hi "  # arg parser splits liberally; document the limit
    assert parent == "there"
    assert force is False


def test_comment_parse_args_missing_msg(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        comment_op.parse_args("1234")
    assert "ERROR" in capsys.readouterr().err


def test_comment_parse_args_empty_msg(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        comment_op.parse_args("1234|   ")
    assert "ERROR" in capsys.readouterr().err


def test_comment_parse_args_message_keeps_colons() -> None:
    """When supertool {args} parts are colon-rejoined in main(), parse_args
    must preserve a colon-bearing body intact (no truncation at first ':').
    """
    aid, msg, parent, force = comment_op.parse_args(
        "1234|Boundary prediction is the right name. Worth saying: it's learnable.|99"
    )
    assert aid == "1234"
    assert msg == "Boundary prediction is the right name. Worth saying: it's learnable."
    assert parent == "99"
    assert force is False


def test_comment_parse_args_force_flag() -> None:
    """4th pipe field 'force' sets force=True."""
    aid, msg, parent, force = comment_op.parse_args("1234|Hello|99|force")
    assert aid == "1234" and msg == "Hello" and parent == "99" and force is True


def test_comment_parse_args_force_no_parent() -> None:
    """force can appear in 4th slot even with empty parent."""
    aid, msg, parent, force = comment_op.parse_args("1234|Hello||force")
    assert parent is None and force is True


def test_resolve_parent_numeric_id_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolver returns numeric id when API+HTML chain succeeds."""
    import io

    api_resp = io.BytesIO(b'{"user": {"username": "alice"}}')
    html_resp = io.BytesIO(b'<div data-id-code="abc" comment-id="1502909"></div>')

    def fake_urlopen(req, timeout=15):
        url = req.full_url if hasattr(req, "full_url") else req
        if "/api/comments/" in url:
            return _CtxRet(api_resp)
        return _CtxRet(html_resp)

    class _CtxRet:
        def __init__(self, body: io.BytesIO) -> None:
            self.body = body

        def __enter__(self):
            return self.body

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(comment_op.urllib.request, "urlopen", fake_urlopen)
    assert comment_op._resolve_parent_numeric_id("abc") == 1502909


def test_resolve_parent_numeric_id_no_username(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    class _Ctx:
        def __init__(self, b): self.b = b
        def __enter__(self): return self.b
        def __exit__(self, *a): return False

    monkeypatch.setattr(
        comment_op.urllib.request,
        "urlopen",
        lambda req, timeout=15: _Ctx(io.BytesIO(b'{"user": null}')),
    )
    assert comment_op._resolve_parent_numeric_id("abc") is None


def test_resolve_parent_numeric_id_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    api_resp = io.BytesIO(b'{"user": {"username": "alice"}}')
    html_resp = io.BytesIO(b'<div>no match here</div>')

    class _Ctx:
        def __init__(self, b): self.b = b
        def __enter__(self): return self.b
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=15):
        url = req.full_url
        return _Ctx(api_resp if "/api/comments/" in url else html_resp)

    monkeypatch.setattr(comment_op.urllib.request, "urlopen", fake_urlopen)
    assert comment_op._resolve_parent_numeric_id("abc") is None


# ---------------------------------------------------------------------------
# Subprocess-level tests: simulate supertool's space-separated argv
# (cmd template '{args}' splits by ':' then space-joins) and verify main's
# rejoin reconstructs the canonical 'aid|MSG|parent' arg intact.
# ---------------------------------------------------------------------------

import subprocess
import sys as _sys


def _run_comment_dryrun(*argv_parts: str) -> tuple[str, str, str | None]:
    """Invoke comment.py main parsing path via real argv, return parse_args result.

    Doesn't post — runs a tiny driver script that imports comment.py, simulates
    the __main__ rejoin, and prints the parsed (aid, msg, parent) tuple.
    """
    repo = Path(__file__).resolve().parents[1]
    driver = (
        "import sys; sys.path.insert(0, %r);\n"
        "import comment as c\n"
        "arg = ':'.join(sys.argv[1:])\n"
        "aid, msg, parent, force = c.parse_args(arg)\n"
        "print(repr((aid, msg, parent)))\n"
    ) % str(repo / "presets" / "devto")
    proc = subprocess.run(
        [_sys.executable, "-c", driver, *argv_parts],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, f"driver failed: {proc.stderr}"
    return eval(proc.stdout.strip())


def test_argv_rejoin_simple() -> None:
    """Body with no ':' → supertool passes ['1234|hi'] → main joins → parse OK."""
    aid, msg, parent = _run_comment_dryrun("1234|hi")
    assert aid == "1234" and msg == "hi" and parent is None


def test_argv_rejoin_one_colon_in_body() -> None:
    """Body with one ':' splits into 2 argv → main rejoins → parse_args sees full body."""
    aid, msg, parent = _run_comment_dryrun("1234|hi", "there")
    assert aid == "1234" and msg == "hi:there" and parent is None


def test_argv_rejoin_multiple_colons_with_parent() -> None:
    """Real-world case: 'aid|MSG: with: colons|parent' → 4 argv parts → rejoin → parse."""
    aid, msg, parent = _run_comment_dryrun("1234|Worth saying", "it's learnable", "and more|99")
    assert aid == "1234"
    assert msg == "Worth saying:it's learnable:and more"
    assert parent == "99"


def test_argv_rejoin_trailing_colon_in_body() -> None:
    """Body ending with ':' → trailing empty arg → rejoin preserves it."""
    aid, msg, parent = _run_comment_dryrun("1234|note", "")
    assert aid == "1234" and msg == "note:"


def test_argv_rejoin_leading_colon_in_body() -> None:
    """Body starting with ':' (rare but legal) — supertool would split as ['1234|', 'rest']."""
    aid, msg, parent = _run_comment_dryrun("1234|", "rest")
    assert aid == "1234" and msg == ":rest"


def test_argv_rejoin_pipes_inside_colon_segment() -> None:
    """Mixed: pipes inside a colon-split segment still parse correctly when rejoined."""
    # supertool split: 'aid|first', 'second|99' → rejoin → 'aid|first:second|99'
    aid, msg, parent = _run_comment_dryrun("1234|first", "second|99")
    assert aid == "1234" and msg == "first:second" and parent == "99"


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


def test_outbound_replied_parent_ids() -> None:
    records = [
        {"comment_id": "c1", "parent_id": "pa"},
        {"comment_id": "c2", "parent_id": "pb"},
        {"comment_id": "c3", "parent_id": None},  # top-level, not a reply
        {"comment_id": "c4"},  # missing
    ]
    assert outbound_op.replied_parent_ids(records) == {"pa", "pb"}


def test_status_since_render_marks_already_replied() -> None:
    articles = [{"id": 100, "title": "T", "url": "https://dev.to/x/t",
                 "public_reactions_count": 0, "comments_count": 2}]
    comments = {100: [
        {"id_code": "c-old-replied", "created_at": "2026-05-01T00:00:00Z",
         "user": {"username": "alice"}, "body_html": "Q"},
        {"id_code": "c-new", "created_at": "2026-05-01T00:00:00Z",
         "user": {"username": "bob"}, "body_html": "Q2"},
    ]}
    out = status_since_op.render(articles, comments,
                                  since="2026-04-30T00:00:00Z",
                                  now="2026-05-01T12:00:00Z",
                                  replied_to={"c-old-replied"})
    assert "c-old-replied] on 'T' (https://dev.to/x/t) (already replied)" in out
    assert "c-new] on 'T' (https://dev.to/x/t)" in out
    # NEXT line still printed for unreplied, suppressed for replied
    assert "devto_comment:100|MSG|c-new" in out
    assert "devto_comment:100|MSG|c-old-replied" not in out


def test_outbound_read_missing_file_returns_empty(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbound_op, "TRACK_FILE", tmp_path / "nope.jsonl")
    assert outbound_op.read() == []


# resolve (article id resolver) -----------------------------------------

def test_resolve_numeric_passthrough() -> None:
    """Numeric input returns int directly — no API call."""
    assert resolve_op.resolve_article_id("123456") == 123456


def test_resolve_slug_calls_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slug 'author/slug' is sent to /articles/author/slug, returns numeric id."""
    calls: list[tuple] = []

    def fake_request(method, path, api_key, body=None, query=None, timeout=30):
        calls.append((method, path))
        return {"id": 999, "title": "T"}

    monkeypatch.setattr(resolve_op, "request", fake_request)
    monkeypatch.setattr(resolve_op, "get_api_key", lambda: "fake-key")
    assert resolve_op.resolve_article_id("alice/some-slug") == 999
    assert calls == [("GET", "/articles/alice/some-slug")]


def test_resolve_url_calls_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full URL parsed to /articles/{author}/{slug}."""
    calls: list[str] = []

    def fake_request(method, path, api_key, body=None, query=None, timeout=30):
        calls.append(path)
        return {"id": 42}

    monkeypatch.setattr(resolve_op, "request", fake_request)
    monkeypatch.setattr(resolve_op, "get_api_key", lambda: "fake-key")
    assert resolve_op.resolve_article_id(
        "https://dev.to/marcosomma/the-real-token-economy-3j3e"
    ) == 42
    assert calls == ["/articles/marcosomma/the-real-token-economy-3j3e"]


def test_resolve_empty_exits(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        resolve_op.resolve_article_id("")
    assert "empty article identifier" in capsys.readouterr().err


def test_resolve_unparseable_exits(capsys: pytest.CaptureFixture[str]) -> None:
    """Input that matches no known form (too-short suffix, no slash, no scheme) → exits."""
    with pytest.raises(SystemExit):
        resolve_op.resolve_article_id("just-a-slug-x")  # suffix only 1 char → no match
    err = capsys.readouterr().err
    assert "cannot parse article identifier" in err
    assert "just-a-slug-x" in err


def test_resolve_bare_slug_calls_list_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare slug matching ^[a-z0-9][a-z0-9-]*-[a-z0-9]{4,6}$ → GET /articles?slug=... → returns id."""
    calls: list[tuple] = []

    def fake_request(method, path, api_key, body=None, query=None, timeout=30):
        calls.append((method, path, query))
        return [{"id": 777, "title": "T"}]

    monkeypatch.setattr(resolve_op, "request", fake_request)
    monkeypatch.setattr(resolve_op, "get_api_key", lambda: "fake-key")

    slug = "the-real-token-economy-is-not-about-spending-less-3j3e"
    result = resolve_op.resolve_article_id(slug)

    assert result == 777
    assert len(calls) == 1
    method, path, query = calls[0]
    assert method == "GET"
    assert path == "/articles"
    assert query == {"per_page": 1, "slug": slug}


def test_resolve_bare_slug_not_found_exits(monkeypatch: pytest.MonkeyPatch,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    """Bare slug → API returns empty list → exits with descriptive error."""
    monkeypatch.setattr(resolve_op, "request", lambda *a, **kw: [])
    monkeypatch.setattr(resolve_op, "get_api_key", lambda: "fake-key")

    with pytest.raises(SystemExit):
        resolve_op.resolve_article_id("my-article-3j3e")
    assert "could not resolve" in capsys.readouterr().err


def test_resolve_bare_slug_short_suffix_exits(capsys: pytest.CaptureFixture[str]) -> None:
    """Suffix of 3 chars or fewer doesn't match the pattern → falls through to unparseable error."""
    with pytest.raises(SystemExit):
        resolve_op.resolve_article_id("my-article-abc")  # 3-char suffix → no match
    assert "cannot parse article identifier" in capsys.readouterr().err


def test_resolve_bare_slug_min_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """4-char suffix is the minimum accepted (boundary)."""
    monkeypatch.setattr(resolve_op, "request", lambda *a, **kw: [{"id": 1}])
    monkeypatch.setattr(resolve_op, "get_api_key", lambda: "fake-key")
    assert resolve_op.resolve_article_id("post-abcd") == 1


def test_resolve_bare_slug_max_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """6-char suffix is the maximum accepted (boundary)."""
    monkeypatch.setattr(resolve_op, "request", lambda *a, **kw: [{"id": 2}])
    monkeypatch.setattr(resolve_op, "get_api_key", lambda: "fake-key")
    assert resolve_op.resolve_article_id("post-abcdef") == 2


def test_resolve_bare_slug_seven_char_suffix_exits(capsys: pytest.CaptureFixture[str]) -> None:
    """7-char suffix exceeds the pattern → falls through to unparseable error."""
    with pytest.raises(SystemExit):
        resolve_op.resolve_article_id("post-abcdefg")
    assert "cannot parse article identifier" in capsys.readouterr().err


def test_resolve_api_returns_no_id_exits(monkeypatch: pytest.MonkeyPatch,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    """API returns dict without 'id' field → exits with descriptive error."""
    monkeypatch.setattr(resolve_op, "request", lambda *a, **kw: {})
    monkeypatch.setattr(resolve_op, "get_api_key", lambda: "fake-key")
    with pytest.raises(SystemExit):
        resolve_op.resolve_article_id("alice/slug")
    assert "could not resolve" in capsys.readouterr().err


# react/comment slug-acceptance integration -----------------------------

def test_react_parse_args_accepts_slug() -> None:
    """parse_args returns the raw identifier untouched — main() resolves it."""
    raw, cat, idempotent = react.parse_args("alice/some-slug")
    assert raw == "alice/some-slug" and cat == "like" and idempotent is True


def test_react_parse_args_accepts_url() -> None:
    raw, cat, idempotent = react.parse_args("https://dev.to/alice/some-slug|unicorn")
    assert raw == "https://dev.to/alice/some-slug" and cat == "unicorn" and idempotent is True


def test_comment_parse_args_accepts_slug() -> None:
    """Comment parse keeps the raw identifier; resolution happens in main()."""
    raw, msg, parent, force = comment_op.parse_args("alice/some-slug|hello")
    assert raw == "alice/some-slug" and msg == "hello" and parent is None
    assert force is False


def test_comment_parse_args_accepts_url() -> None:
    raw, msg, parent, force = comment_op.parse_args(
        "https://dev.to/alice/some-slug|hi|99"
    )
    assert raw == "https://dev.to/alice/some-slug"
    assert msg == "hi" and parent == "99"
    assert force is False
