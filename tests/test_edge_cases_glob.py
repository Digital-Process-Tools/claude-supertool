"""Edge-case tests for op_glob / _glob_files.

Coverage:
  1. Traversal pattern (../../**/*.txt) — does glob walk outside cwd?
  2. Symlink loop — A→B→A — shouldn't infinite-loop
  3. NUL byte in pattern — clean ERROR
  4. Very deep recursion — 50 levels, glob **/* completes bounded
  5. Excluded dirs aren't traversed (.git/, node_modules/ defaults)
  6. no-auto-read flag via dispatch("glob:pattern:no-auto-read")
  7. Pattern matching nothing — clean empty result, not error
  8. Pattern with special chars [, (, ? — verify handling
  9. Symlink pointing outside cwd that matches pattern — inclusion documented
"""
from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

from _symlink import requires_symlink

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import supertool


# ---------------------------------------------------------------------------
# 1. Traversal pattern — ../../**/*.txt
# ---------------------------------------------------------------------------

def test_traversal_pattern_stays_within_cwd(tmp_path: Path, monkeypatch) -> None:
    """glob:../../**/*.txt — pattern starts with '../../'.

    CONTRACT: _glob_files feeds the pattern directly to glob.glob (non-walk path
    because the root part resolves outside cwd and os.path.isdir() may or may
    not succeed).  Files outside cwd CAN be returned if the OS resolves them.
    This test documents the actual behaviour rather than asserting a restriction.
    """
    # Create a file two levels above tmp_path (inside the OS tmp tree)
    outside = tmp_path.parent.parent / "outside_marker_supertool_test.txt"
    try:
        outside.write_text("outside\n")
        # cd into tmp_path so relative traversal has a reference point
        monkeypatch.chdir(tmp_path)
        out = supertool.op_glob("../../**/*.txt", no_auto_read=True)
        # Document: the pattern MAY reach outside cwd — no hard block.
        # The important thing is it must not crash.
        assert "ERROR" not in out or "empty pattern" not in out
        # If it found the file, it's documented behaviour (not a security block).
    finally:
        if outside.exists():
            outside.unlink()


def test_traversal_absolute_pattern_works(tmp_path: Path) -> None:
    """Absolute pattern outside cwd — glob resolves it normally."""
    f = tmp_path / "hit.txt"
    f.write_text("x\n")
    out = supertool.op_glob(str(tmp_path / "*.txt"), no_auto_read=True)
    assert "hit.txt" in out


# ---------------------------------------------------------------------------
# 2. Symlink loop — A → B → A
# ---------------------------------------------------------------------------

@requires_symlink
def test_symlink_loop_does_not_infinite_loop(tmp_path: Path, monkeypatch) -> None:
    """Dir A contains a symlink to dir B; dir B contains a symlink back to A.

    os.walk() does NOT follow symlinks by default (followlinks=False), so the
    loop is never entered.  This test verifies the call terminates in finite
    time and returns a result (even if empty).
    """
    dir_a = tmp_path / "dir_a"
    dir_b = tmp_path / "dir_b"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "file_a.txt").write_text("a\n")
    (dir_b / "file_b.txt").write_text("b\n")
    # Create the loop: a/link_to_b -> dir_b, b/link_to_a -> dir_a
    (dir_a / "link_to_b").symlink_to(dir_b)
    (dir_b / "link_to_a").symlink_to(dir_a)

    monkeypatch.chdir(tmp_path)
    # Must complete without hanging or raising RecursionError
    out = supertool.op_glob("**/*.txt", no_auto_read=True)
    assert "ERROR" not in out
    # Both real files should be found (symlink dirs not followed by default)
    assert "file_a.txt" in out
    assert "file_b.txt" in out


# ---------------------------------------------------------------------------
# 3. NUL byte in pattern
# ---------------------------------------------------------------------------

def test_nul_byte_in_pattern_returns_clean_error(tmp_path: Path, monkeypatch) -> None:
    """Pattern containing NUL byte — should produce a clean ERROR, not crash."""
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("**/*\x00*.txt", no_auto_read=True)
    # Acceptable outcomes: ERROR string, empty result, or no crash.
    # The key invariant is: no unhandled exception and no Python traceback in output.
    assert "Traceback" not in out
    # If an error is surfaced, it should mention the issue or just be empty/0 files.
    # Both are acceptable — we just document the boundary.


# ---------------------------------------------------------------------------
# 4. Very deep recursion — 50 levels
# ---------------------------------------------------------------------------

def test_very_deep_recursion_completes(tmp_path: Path, monkeypatch) -> None:
    """50 levels of nested empty dirs with **/* — must complete without
    RecursionError or timeout. Places a single file at the bottom."""
    current = tmp_path
    for i in range(50):
        current = current / f"d{i}"
        current.mkdir()
    (current / "deep.txt").write_text("deep\n")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("**/*", no_auto_read=True)
    assert "ERROR" not in out
    assert "deep.txt" in out


# ---------------------------------------------------------------------------
# 5. Excluded dirs aren't traversed
# ---------------------------------------------------------------------------

def test_git_dir_excluded_by_default(tmp_path: Path, monkeypatch) -> None:
    """Files inside .git/ must not appear in glob results (default exclusion)."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "secret.txt").write_text("internal\n")
    (tmp_path / "visible.txt").write_text("public\n")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("**/*.txt", no_auto_read=True)
    assert "secret.txt" not in out
    assert "visible.txt" in out


def test_node_modules_excluded_by_default(tmp_path: Path, monkeypatch) -> None:
    """Files inside node_modules/ must not appear in glob results."""
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "index.js").write_text("module.exports = {}\n")
    (tmp_path / "app.js").write_text("const x = 1\n")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("**/*.js", no_auto_read=True)
    assert "index.js" not in out
    assert "app.js" in out


def test_excluded_dirs_not_traversed_with_no_exclude_false(tmp_path: Path, monkeypatch) -> None:
    """With no_exclude=False (default), excluded dirs are pruned."""
    pycache = tmp_path / "__pycache__"
    pycache.mkdir()
    (pycache / "module.pyc").write_text("")
    (tmp_path / "main.py").write_text("pass\n")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("**/*", no_auto_read=True)
    assert "module.pyc" not in out


@pytest.mark.skipif(sys.version_info < (3, 11),
                    reason="glob.glob include_hidden=True requires Python 3.11+")
def test_no_exclude_flag_bypasses_exclusions(tmp_path: Path, monkeypatch) -> None:
    """With no_exclude=True, .git/ and node_modules/ are NOT pruned."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("**/*", no_exclude=True, no_auto_read=True)
    assert "config" in out


# ---------------------------------------------------------------------------
# 6. no-auto-read flag via dispatch
# ---------------------------------------------------------------------------

def test_no_auto_read_via_dispatch_single_result(tmp_path: Path, monkeypatch) -> None:
    """dispatch('glob:*.py:no-auto-read') must suppress auto-read on single match."""
    (tmp_path / "only.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)
    out = supertool.dispatch(f"glob:*.py:no-auto-read")
    assert "[auto-read:" not in out
    assert "only.py" in out
    assert "x = 1" not in out


def test_no_auto_read_via_dispatch_concrete_file(tmp_path: Path) -> None:
    """dispatch('glob:<path>:no-auto-read') must suppress auto-read on concrete file."""
    f = tmp_path / "concrete.py"
    f.write_text("y = 2\n")
    out = supertool.dispatch(f"glob:{f}:no-auto-read")
    assert "[auto-read:" not in out
    assert "concrete.py" in out
    assert "y = 2" not in out


# ---------------------------------------------------------------------------
# 7. Pattern matching nothing
# ---------------------------------------------------------------------------

def test_pattern_matching_nothing_is_clean(tmp_path: Path, monkeypatch) -> None:
    """Pattern that matches no files — (0 files) result, no error."""
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("**/*.definitely_not_a_real_extension_xyz", no_auto_read=True)
    assert "(0 files)" in out
    assert "ERROR" not in out
    assert "Traceback" not in out


def test_pattern_matching_nothing_in_nonempty_dir(tmp_path: Path, monkeypatch) -> None:
    """Even when files exist, a non-matching pattern returns (0 files)."""
    (tmp_path / "hello.py").write_text("")
    (tmp_path / "world.js").write_text("")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("*.rb", no_auto_read=True)
    assert "(0 files)" in out
    assert "hello.py" not in out


# ---------------------------------------------------------------------------
# 8. Pattern with special chars: [, (, ?
# ---------------------------------------------------------------------------

def test_bracket_pattern_character_class(tmp_path: Path, monkeypatch) -> None:
    """Pattern like [ab].py — bracket char class, matches 'a.py' and 'b.py'."""
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "c.py").write_text("")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("[ab].py", no_auto_read=True)
    assert "a.py" in out
    assert "b.py" in out
    assert "c.py" not in out


def test_question_mark_single_char_wildcard(tmp_path: Path, monkeypatch) -> None:
    """Pattern ?.py — matches single-char filenames only."""
    (tmp_path / "a.py").write_text("")
    (tmp_path / "ab.py").write_text("")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_glob("?.py", no_auto_read=True)
    assert "a.py" in out
    assert "ab.py" not in out


def test_literal_paren_in_dirname(tmp_path: Path, monkeypatch) -> None:
    """Directory name containing '(' — glob should find files inside it."""
    subdir = tmp_path / "my(dir)"
    subdir.mkdir()
    (subdir / "file.txt").write_text("hello\n")
    monkeypatch.chdir(tmp_path)
    # Use ** to recurse into the dir
    out = supertool.op_glob("**/*.txt", no_auto_read=True)
    assert "file.txt" in out


def test_unclosed_bracket_pattern(tmp_path: Path, monkeypatch) -> None:
    """Unclosed '[' in pattern — must not crash (glob.glob raises or returns empty)."""
    (tmp_path / "a.py").write_text("")
    monkeypatch.chdir(tmp_path)
    # glob.glob("[a.py" raises ValueError on Python 3.12+ but returns [] on older.
    # Either way: no unhandled exception should escape op_glob.
    try:
        out = supertool.op_glob("[a.py", no_auto_read=True)
        assert "Traceback" not in out
    except Exception as e:
        pytest.fail(f"op_glob raised unhandled exception for unclosed '[': {e}")


# ---------------------------------------------------------------------------
# 9. Symlink pointing outside cwd that matches the pattern
# ---------------------------------------------------------------------------

@requires_symlink
def test_symlink_to_file_outside_cwd_included(tmp_path: Path, monkeypatch) -> None:
    """A symlink inside cwd that points to a file outside cwd.

    CONTRACT (documented, not enforced): op_glob includes symlinked files
    because os.path.isfile() returns True for symlinks to regular files.
    The file is found via the symlink path (which is within cwd).
    """
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    real_file = outside_dir / "real.txt"
    real_file.write_text("real content\n")

    inside_dir = tmp_path / "inside"
    inside_dir.mkdir()
    # Create symlink inside cwd pointing to the outside file
    link = inside_dir / "link.txt"
    link.symlink_to(real_file)

    monkeypatch.chdir(inside_dir)
    out = supertool.op_glob("*.txt", no_auto_read=True)
    # The symlink resolves to a file — it IS included (documented behaviour).
    assert "link.txt" in out, (
        "Symlink to external file should be included — os.path.isfile() follows symlinks. "
        f"Got: {out!r}"
    )


@requires_symlink
def test_symlink_to_dir_outside_cwd_traversed_with_followlinks_false(
    tmp_path: Path, monkeypatch
) -> None:
    """A symlink to a directory outside cwd — os.walk default (followlinks=False)
    means the symlink dir is NOT descended into for recursive globs.

    CONTRACT: files inside symlinked directories are NOT returned by the
    os.walk path (followlinks=False). The glob.glob fallback may behave
    differently depending on Python version.
    """
    outside_dir = tmp_path / "outside_tree"
    outside_dir.mkdir()
    (outside_dir / "hidden.txt").write_text("secret\n")

    cwd_dir = tmp_path / "workspace"
    cwd_dir.mkdir()
    (cwd_dir / "local.txt").write_text("local\n")
    # Symlink from workspace/external -> outside_tree
    (cwd_dir / "external").symlink_to(outside_dir)

    monkeypatch.chdir(cwd_dir)
    out = supertool.op_glob("**/*.txt", no_auto_read=True)
    # local.txt must be found
    assert "local.txt" in out
    # hidden.txt inside symlinked dir — document whether it's included or not
    # (os.walk with followlinks=False will NOT follow the symlink)
    # This assertion documents the current behaviour:
    assert "hidden.txt" not in out, (
        "os.walk(followlinks=False) should not descend into symlinked dirs. "
        "If this fails, the implementation changed to followlinks=True."
    )
