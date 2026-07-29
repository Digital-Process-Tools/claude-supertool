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
    `_auth` / `_outbound` shims don't bleed into the test module.

    **The isolation is scoped to the exec and then undone.** It used to be a
    permanent, process-global `sys.path[:] = ...` that dropped *every* entry
    containing `presets/` — a filter far wider than the shim collision it
    guards against, and one this file has no standing to impose on the rest of
    the session. `presets/mcp` was collateral.

    That broke a neighbour rather than this file. `test_mcp_stop_outcome_547.py`
    and `test_mcp_status_unknown_549.py` both put `presets/mcp` on the path at
    import time and then `import stop` / `import status` *lazily, inside a
    fixture* — so the entry has to survive until the test runs, not merely
    until collection ends. Any worker that ran this file first left them with
    `ModuleNotFoundError: No module named 'stop'`. Fully deterministic given
    the order (`pytest tests/test_comment_file.py
    tests/test_mcp_stop_outcome_547.py -p no:randomly -n0` reproduces it), and
    invisible without it, because xdist only sometimes puts that pair on one
    worker — which is why it surfaced on one leg of one PR and had been latent
    on master since #548.

    Restoring is safe because the shim collision only exists while
    `exec_module` runs: by the time the path goes back, the module has bound
    the shims it imported.
    """
    preset_dir = REPO_ROOT / "presets" / preset
    saved_path = sys.path[:]
    for shim in ("_auth", "_outbound", "_resolve", "_session", "_rest",
                 "_graphql", "_atproto", "_me"):
        sys.modules.pop(shim, None)
    sys.path[:] = [p for p in sys.path
                   if "presets/" not in p.replace("\\", "/")]
    sys.path.insert(0, str(preset_dir))
    try:
        spec = importlib.util.spec_from_file_location(
            f"{preset}_{name}", preset_dir / f"{name}.py")
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved_path
    return mod


def test_load_leaves_sys_path_as_it_found_it() -> None:
    """`_load`'s isolation must not outlive `_load`.

    The regression this pins is not in this file: a permanent global filter
    here removed `presets/mcp` from the path and broke the lazy `import stop`
    in `test_mcp_stop_outcome_547.py` whenever a worker ran the two together.
    Asserted on the real entry that was collateral, not only on equality, so a
    narrower-but-still-permanent filter cannot pass this either.
    """
    marker = str(REPO_ROOT / "presets" / "mcp")
    sys.path.insert(0, marker)
    try:
        before = sys.path[:]
        _load("devto", "comment")
        assert sys.path == before, "sys.path escaped _load"
        assert marker in sys.path, "_load stripped another module's preset dir"
    finally:
        if marker in sys.path:
            sys.path.remove(marker)


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
    # #149: ALSO rejected by the publish-body allowlist before the
    # existence check. Either message is acceptable — both signal abort.
    assert "file not found" in err or "escapes the safety allowlist" in err


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
    # #149: ALSO rejected by allowlist before existence check.
    assert "file not found" in err or "escapes the safety allowlist" in err


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
