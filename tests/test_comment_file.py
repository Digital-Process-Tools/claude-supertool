"""Tests for body-file support in devto/hashnode comment + reply ops.

Mirrors the bluesky_publish convention: when the MESSAGE argument is a path to
an existing file, parse_args reads the file contents into the message body.
This keeps long multi-paragraph drafts out of the supertool tokenizer.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(preset: str, name: str):
    """Load a preset module fresh, isolating sys.path so other presets'
    `_auth` / `_outbound` shims don't bleed into the test module."""
    preset_dir = REPO_ROOT / "presets" / preset
    for shim in ("_auth", "_outbound", "_resolve", "_session", "_rest",
                 "_graphql", "_atproto", "_me"):
        sys.modules.pop(shim, None)
    sys.path[:] = [p for p in sys.path
                   if "presets/" not in p.replace("\\", "/")]
    sys.path.insert(0, str(preset_dir))
    spec = importlib.util.spec_from_file_location(
        f"{preset}_{name}", preset_dir / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- devto/comment.py ------------------------------------------------


def test_devto_comment_inline_message() -> None:
    mod = _load("devto", "comment")
    raw, message, parent, force = mod.parse_args("123|Hello world")
    assert raw == "123"
    assert message == "Hello world"
    assert parent is None
    assert force is False


def test_devto_comment_file_path(tmp_path: Path) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text("Multi\nparagraph\ndraft.")
    mod = _load("devto", "comment")
    raw, message, parent, force = mod.parse_args(f"123|{body_file}")
    assert raw == "123"
    assert message == "Multi\nparagraph\ndraft."
    assert parent is None
    assert force is False


def test_devto_comment_file_path_with_parent(tmp_path: Path) -> None:
    body_file = tmp_path / "reply.md"
    body_file.write_text("Reply body")
    mod = _load("devto", "comment")
    raw, message, parent, force = mod.parse_args(f"123|{body_file}|abc12")
    assert message == "Reply body"
    assert parent == "abc12"


def test_devto_comment_inline_with_pipe_in_text() -> None:
    """split('|') splits aggressively; ensure inline text without file shape
    still works for normal short bodies."""
    mod = _load("devto", "comment")
    raw, message, _, _ = mod.parse_args("123|Just text")
    assert message == "Just text"


def test_devto_comment_nonexistent_path_treated_as_text() -> None:
    """A path-shaped message that doesn't point to a real file should pass
    through as inline text (so users typing path-like prose aren't surprised)."""
    mod = _load("devto", "comment")
    raw, message, _, _ = mod.parse_args("123|/nonexistent/path/to/file.md")
    assert message == "/nonexistent/path/to/file.md"


# ---------- hashnode/comment.py --------------------------------------------


def test_hashnode_comment_inline_message() -> None:
    mod = _load("hashnode", "comment")
    post, message, force = mod.parse_args("post-id|Hello world")
    assert post == "post-id"
    assert message == "Hello world"
    assert force is False


def test_hashnode_comment_file_path(tmp_path: Path) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text("Multi\nparagraph\ndraft.")
    mod = _load("hashnode", "comment")
    post, message, force = mod.parse_args(f"post-id|{body_file}")
    assert post == "post-id"
    assert message == "Multi\nparagraph\ndraft."
    assert force is False


def test_hashnode_comment_force_after_file(tmp_path: Path) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text("Body")
    mod = _load("hashnode", "comment")
    post, message, force = mod.parse_args(f"post-id|{body_file}|force")
    assert message == "Body"
    assert force is True


def test_hashnode_comment_nonexistent_path_treated_as_text() -> None:
    mod = _load("hashnode", "comment")
    post, message, _ = mod.parse_args("post-id|/nonexistent/file.md")
    assert message == "/nonexistent/file.md"


# ---------- hashnode/reply.py ----------------------------------------------


def test_hashnode_reply_inline_message() -> None:
    mod = _load("hashnode", "reply")
    cid, message = mod.parse_args("comm-7|Good point")
    assert cid == "comm-7"
    assert message == "Good point"


def test_hashnode_reply_file_path(tmp_path: Path) -> None:
    body_file = tmp_path / "reply.md"
    body_file.write_text("Long\nreply\nbody.")
    mod = _load("hashnode", "reply")
    cid, message = mod.parse_args(f"comm-7|{body_file}")
    assert cid == "comm-7"
    assert message == "Long\nreply\nbody."


def test_hashnode_reply_nonexistent_path_treated_as_text() -> None:
    mod = _load("hashnode", "reply")
    cid, message = mod.parse_args("comm-7|/nonexistent/file.md")
    assert message == "/nonexistent/file.md"


# ---------- error handling --------------------------------------------------


def test_devto_comment_empty_args_errors() -> None:
    mod = _load("devto", "comment")
    with pytest.raises(SystemExit):
        mod.parse_args("|")


def test_hashnode_comment_empty_args_errors() -> None:
    mod = _load("hashnode", "comment")
    with pytest.raises(SystemExit):
        mod.parse_args("post-id|")


def test_hashnode_reply_empty_args_errors() -> None:
    mod = _load("hashnode", "reply")
    with pytest.raises(SystemExit):
        mod.parse_args("comm-7|")


# ---------- file:// prefix --------------------------------------------------


def test_devto_comment_file_prefix(tmp_path: Path) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text("Body via file:// prefix")
    mod = _load("devto", "comment")
    raw, message, _, _ = mod.parse_args(f"123|file://{body_file}")
    assert message == "Body via file:// prefix"


def test_devto_comment_file_prefix_missing_errors(capsys: pytest.CaptureFixture[str]) -> None:
    mod = _load("devto", "comment")
    with pytest.raises(SystemExit):
        mod.parse_args("123|file:///nonexistent/typo.md")
    err = capsys.readouterr().err
    assert "file not found" in err
    assert "file:// prefix requires" in err


def test_hashnode_comment_file_prefix(tmp_path: Path) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text("Hashnode body via prefix")
    mod = _load("hashnode", "comment")
    post, message, _ = mod.parse_args(f"post-id|file://{body_file}")
    assert message == "Hashnode body via prefix"


def test_hashnode_comment_file_prefix_missing_errors(capsys: pytest.CaptureFixture[str]) -> None:
    mod = _load("hashnode", "comment")
    with pytest.raises(SystemExit):
        mod.parse_args("post-id|file:///nonexistent/typo.md")
    err = capsys.readouterr().err
    assert "file not found" in err


def test_hashnode_reply_file_prefix(tmp_path: Path) -> None:
    body_file = tmp_path / "reply.md"
    body_file.write_text("Reply via prefix")
    mod = _load("hashnode", "reply")
    cid, message = mod.parse_args(f"comm-7|file://{body_file}")
    assert message == "Reply via prefix"


def test_hashnode_reply_file_prefix_missing_errors(capsys: pytest.CaptureFixture[str]) -> None:
    mod = _load("hashnode", "reply")
    with pytest.raises(SystemExit):
        mod.parse_args("comm-7|file:///nonexistent/typo.md")
    err = capsys.readouterr().err
    assert "file not found" in err


def test_bluesky_publish_file_prefix(tmp_path: Path) -> None:
    body_file = tmp_path / "post.txt"
    body_file.write_text("Bluesky body via prefix")
    bluesky_mod = _load("bluesky", "publish")
    body, _, _ = bluesky_mod.parse_args(f"file://{body_file}")
    assert body == "Bluesky body via prefix"


def test_bluesky_publish_file_prefix_missing_errors(capsys: pytest.CaptureFixture[str]) -> None:
    bluesky_mod = _load("bluesky", "publish")
    with pytest.raises(SystemExit):
        bluesky_mod.parse_args("file:///nonexistent/typo.txt")
    err = capsys.readouterr().err
    assert "file not found" in err


# ---------- hashnode auto_force env var -------------------------------------


def test_hashnode_react_auto_force_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """SUPERTOOL_AUTO_FORCE truthy makes parse_args return force=True without |force."""
    monkeypatch.setenv("SUPERTOOL_AUTO_FORCE", "true")
    mod = _load("hashnode", "react")
    raw, force = mod.parse_args("post-id")
    assert force is True


def test_hashnode_react_auto_force_off_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without env var, no |force = force False (current behavior)."""
    monkeypatch.delenv("SUPERTOOL_AUTO_FORCE", raising=False)
    mod = _load("hashnode", "react")
    raw, force = mod.parse_args("post-id")
    assert force is False


def test_hashnode_react_auto_force_explicit_force_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPERTOOL_AUTO_FORCE", raising=False)
    mod = _load("hashnode", "react")
    raw, force = mod.parse_args("post-id|force")
    assert force is True


def test_hashnode_react_auto_force_falsy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """SUPERTOOL_AUTO_FORCE=false should NOT trigger auto-force."""
    monkeypatch.setenv("SUPERTOOL_AUTO_FORCE", "false")
    mod = _load("hashnode", "react")
    raw, force = mod.parse_args("post-id")
    assert force is False


def test_hashnode_comment_auto_force_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERTOOL_AUTO_FORCE", "1")
    mod = _load("hashnode", "comment")
    post, message, force = mod.parse_args("post-id|hello")
    assert force is True
