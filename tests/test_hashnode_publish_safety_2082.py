"""Regression tests for #2082: hashnode body-file reads bypass the #149 allowlist.

`presets/hashnode/{comment,reply,publish}.py` read a bare or `file://` body
path straight off disk with no call into `_publish_safety.safe_resolve_body_path`,
while their bluesky/devto twins route through it (`presets/bluesky/publish.py`,
`presets/devto/{comment,publish}.py`). This file proves both directions: a path
outside the `.max/`/`drafts/`/`posts/`/`blog/` allowlist is refused, and a path
inside it still reads -- the "must not fire" case is paired with a "must fire"
case in the same fixture so a fixture that never reaches the read cannot pass
this vacuously.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "presets"))
import _publish_safety  # noqa: E402
from _preset_loader import load_preset_module  # noqa: E402

comment_op = load_preset_module("hashnode", "comment", "hn_")
reply_op = load_preset_module("hashnode", "reply", "hn_")
publish_op = load_preset_module("hashnode", "publish", "hn_")


@pytest.fixture
def strict_publish(monkeypatch, tmp_path):
    """Force strict allowlist mode, isolated from this checkout's own cwd.

    Mirrors `tests/test_security_publish_149.py::strict_publish` -- both env
    and cwd feed the gate (`.supertool.json` is found by walking up from cwd),
    so both must be isolated or "strict" silently reads whatever this
    checkout's own config says.
    """
    monkeypatch.delenv("SUPERTOOL_PUBLISH_BODY_ALLOWLIST", raising=False)
    monkeypatch.chdir(tmp_path)
    if hasattr(_publish_safety, "_CACHED_CONFIG"):
        delattr(_publish_safety, "_CACHED_CONFIG")
    yield
    if hasattr(_publish_safety, "_CACHED_CONFIG"):
        delattr(_publish_safety, "_CACHED_CONFIG")


class TestCommentBodyAllowlist:
    def test_bare_path_outside_allowlist_is_rejected(self, strict_publish, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text("SECRET body")
        with pytest.raises(SystemExit):
            comment_op._resolve_body(str(outside))

    def test_file_prefix_outside_allowlist_is_rejected(self, strict_publish, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text("SECRET body")
        with pytest.raises(SystemExit):
            comment_op._resolve_body(f"file://{outside}")

    def test_bare_path_inside_allowlist_still_reads(self, strict_publish, tmp_path):
        (tmp_path / "drafts").mkdir()
        body = tmp_path / "drafts" / "post.md"
        body.write_text("hello from drafts")
        text, from_file = comment_op._resolve_body(str(body))
        assert from_file is True
        assert text == "hello from drafts"


class TestReplyBodyAllowlist:
    def test_bare_path_outside_allowlist_is_rejected(self, strict_publish, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text("SECRET body")
        with pytest.raises(SystemExit):
            reply_op._resolve_body(str(outside))

    def test_file_prefix_outside_allowlist_is_rejected(self, strict_publish, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text("SECRET body")
        with pytest.raises(SystemExit):
            reply_op._resolve_body(f"file://{outside}")

    def test_bare_path_inside_allowlist_still_reads(self, strict_publish, tmp_path):
        (tmp_path / ".max").mkdir()
        body = tmp_path / ".max" / "reply.md"
        body.write_text("hello from .max")
        text, from_file = reply_op._resolve_body(str(body))
        assert from_file is True
        assert text == "hello from .max"


class TestPublishBodyAllowlist:
    def test_bare_path_outside_allowlist_is_rejected(self, strict_publish, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text("SECRET body")
        with pytest.raises(SystemExit):
            publish_op.parse_args(f"T|{outside}|https://x.io")

    def test_file_prefix_outside_allowlist_is_rejected(self, strict_publish, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text("SECRET body")
        with pytest.raises(SystemExit):
            publish_op.parse_args(f"T|file://{outside}|https://x.io")

    def test_bare_path_inside_allowlist_still_reads(self, strict_publish, tmp_path):
        (tmp_path / "posts").mkdir()
        body = tmp_path / "posts" / "post.md"
        body.write_text("hello from posts")
        parsed = publish_op.parse_args(f"T|{body}|https://x.io")
        assert parsed["markdown"] == "hello from posts"
