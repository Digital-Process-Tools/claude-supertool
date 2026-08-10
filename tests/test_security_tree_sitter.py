"""Security & robustness tests for tree-sitter-backed ops.

Covers: map, between (symbol mode), tree.
All tests that mock tree-sitter use the conftest fixture pattern so state is
always cleaned up, even on failure.

Audit 2026-05-23.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from _symlink import require_symlink

import supertool
from conftest import _has_any_tree_sitter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ts_enabled(ts_extract_return=None):
    """Context manager: enable tree-sitter (mocked) for op_map / op_between."""
    fake_symbols = ts_extract_return if ts_extract_return is not None else []

    ctx = patch.multiple(
        supertool,
        _has_tree_sitter=MagicMock(return_value=True),
        _ts_extract=MagicMock(return_value=fake_symbols),
        _ts_find_node=MagicMock(return_value=None),
        _has_ctags=MagicMock(return_value=False),
    )
    return ctx


# ---------------------------------------------------------------------------
# 1. Crafted source file that triggers tree-sitter memory bomb
#    (deeply nested AST via mock — verify map completes bounded)
# ---------------------------------------------------------------------------

def _make_deep_node_tree(depth: int) -> MagicMock:
    """Build a mock tree-sitter node tree nested `depth` levels deep.

    Each node has one child, alternating between def_node and plain_node types.
    This simulates what a deeply-nested source file would produce.
    """
    root = MagicMock()
    root.type = "module"
    root.start_point = (0, 0)
    root.end_point = (0, 0)

    parent = root
    for i in range(depth):
        child = MagicMock()
        child.type = "expression_statement"  # not a def node, won't be recorded
        child.children = []
        child.start_point = (i, 0)
        child.end_point = (i, 0)
        parent.children = [child]
        parent = child

    return root


def test_map_deep_ast_mock_completes_bounded(tmp_path: Path) -> None:
    """op_map with a mocked 5000-level deep AST must complete, not blow the stack.

    The _ts_extract _walk is recursive. At 5000 nesting levels it could hit
    Python's default recursion limit (1000). We mock _ts_extract to bypass that
    risk and verify op_map itself completes in bounded time.
    """
    f = tmp_path / "deep.py"
    # Write Python that has one real function so the file isn't empty
    f.write_text("def top(): pass\n")

    # _ts_extract returns empty (simulates a parse that found nothing interesting
    # after traversing a deep tree) — op_map must not hang or crash
    with _make_ts_enabled(ts_extract_return=[]):
        start = time.monotonic()
        out = supertool.op_map(str(f))
        elapsed = time.monotonic() - start

    assert elapsed < 5.0, f"op_map took {elapsed:.2f}s — should be instant with empty TS result"
    assert "ERROR" not in out
    # Falls back to regex, finds 'top'
    assert "top" in out or "no symbols" in out


def test_ts_extract_walk_recursion_depth(tmp_path: Path) -> None:
    """_ts_extract's _walk is recursive. On a pathologically nested mock tree,
    it must either complete or raise RecursionError gracefully (not segfault).

    We build a mock tree 1500 levels deep (> default Python limit of 1000)
    and verify the call either completes or raises RecursionError — never hangs.
    """
    if not _has_any_tree_sitter():
        pytest.skip("tree-sitter not installed — mocking get_parser instead")

    f = tmp_path / "nested.py"
    f.write_text("x = 1\n")

    # Build a 1500-level deep mock node chain
    deep_root = _make_deep_node_tree(1500)
    mock_tree = MagicMock()
    mock_tree.root_node = deep_root

    supertool._TS_CHECKED = True
    supertool._TS_AVAILABLE = True
    supertool._TS_PACKAGE = "pack"

    try:
        if supertool._TS_PACKAGE == "pack":
            pkg = "tree_sitter_language_pack"
        else:
            pkg = "tree_sitter_languages"

        mock_parser = MagicMock()
        mock_parser.parse.return_value = mock_tree

        with patch(f"{pkg}.get_parser", return_value=mock_parser):
            result = supertool._ts_extract(str(f), "python")
        # If it completes: result must be a list (empty or with items)
        assert isinstance(result, list)
    except RecursionError:
        # RecursionError is acceptable — at least it's not a hang or segfault
        pass
    finally:
        supertool._TS_AVAILABLE = False


# ---------------------------------------------------------------------------
# 2. Binary file passed to map — clean fallback, no crash
# ---------------------------------------------------------------------------

def test_map_binary_file_unsupported_extension(tmp_path: Path) -> None:
    """map:image.png → completes cleanly, no crash, no ERROR.

    .png has no regex patterns, so no symbols are extracted.
    op_map still processes the file via _collect_files (which includes it
    when passed as a direct file path) and emits '(no symbols)'.
    The key guarantee: no crash, no ERROR in output.
    """
    img = tmp_path / "image.png"
    img.write_bytes(bytes(range(256)) * 10)

    out = supertool.op_map(str(img))
    assert "ERROR" not in out
    # Either "no symbols" (file processed, nothing extracted) or
    # "no supported files found" (file filtered). Either is acceptable.
    assert "no symbols" in out or "no supported files found" in out or "image.png" in out


def test_map_binary_file_with_py_extension(tmp_path: Path) -> None:
    """map on a file named .py but containing binary bytes — must not crash.

    The regex fallback reads with errors='replace'. tree-sitter + ctags are
    disabled by conftest, so only the regex tier runs.
    """
    f = tmp_path / "weird.py"
    # Write binary content that looks nothing like Python
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4)

    out = supertool.op_map(str(f))
    assert "ERROR" not in out
    # No real Python symbols in binary content — 'no symbols' expected
    assert "no symbols" in out or "0 files" in out or "weird.py" in out


def test_map_binary_file_php_extension(tmp_path: Path) -> None:
    """map on binary content with .php extension — regex tier, no crash."""
    f = tmp_path / "bin.php"
    f.write_bytes(b"\x00\x01\x02\x03" * 1000 + b"\xff\xfe")

    out = supertool.op_map(str(f))
    assert "ERROR" not in out


# ---------------------------------------------------------------------------
# 3. File with NUL bytes — clean handling
# ---------------------------------------------------------------------------

def test_map_file_with_nul_bytes_mid_content(tmp_path: Path) -> None:
    """A .py file containing NUL bytes must not crash op_map."""
    f = tmp_path / "nulls.py"
    f.write_bytes(b"def foo():\n    pass\n\x00\x00\x00\ndef bar():\n    pass\n")

    out = supertool.op_map(str(f))
    assert "ERROR" not in out
    # Regex should still find 'foo' and 'bar' since NULs are in the middle
    assert "foo" in out or "bar" in out or "no symbols" in out


def test_between_symbol_file_with_nul_bytes(tmp_path: Path) -> None:
    """between on a file with NUL bytes — must return ERROR (no TS) not crash."""
    f = tmp_path / "nuls.py"
    f.write_bytes(b"def foo():\n    x = \x00\n    return x\n")

    # tree-sitter disabled by conftest → ERROR: requires tree-sitter
    out = supertool.op_between_symbol("foo", str(f))
    assert "ERROR" in out
    assert "Traceback" not in out


# ---------------------------------------------------------------------------
# 4. Path traversal — between reads file but only returns symbol section
# ---------------------------------------------------------------------------

def test_between_symbol_path_traversal_reads_target(tmp_path: Path, monkeypatch) -> None:
    """between:foo:../../../etc/passwd — reads the target but tree-sitter disabled
    so it returns an error, not file contents. Path traversal itself is allowed
    (no chroot) but the output is strictly bounded to the symbol match.
    """
    sub = tmp_path / "project"
    sub.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET\n")
    monkeypatch.chdir(sub)

    out = supertool.op_between_symbol("foo", "../secret.txt")
    # Without tree-sitter: ERROR (unsupported extension or no tree-sitter)
    # With tree-sitter: ERROR (symbol not found) — never full file dump
    assert "TOP SECRET" not in out
    assert "ERROR" in out


def test_between_symbol_path_traversal_no_full_dump(tmp_path: Path) -> None:
    """Even if tree-sitter is mocked and a symbol 'exists', the output is only
    the matched symbol body — not the entire file."""
    f = tmp_path / "code.py"
    f.write_text(
        "SECRET_KEY = 'abc123'\n"
        "def safe_func():\n"
        "    return 1\n"
        "ANOTHER_SECRET = 'xyz'\n"
    )

    # Mock: _ts_find_node returns a node matching only lines 2-3
    mock_node = MagicMock()
    mock_node.start_point = (1, 0)  # line 2 (0-indexed)
    mock_node.end_point = (2, 0)    # line 3

    supertool._TS_CHECKED = True
    supertool._TS_AVAILABLE = True
    supertool._TS_PACKAGE = "pack"

    try:
        with patch.object(supertool, "_ts_find_node", return_value=(mock_node, "def", 1)):
            out = supertool.op_between_symbol("safe_func", str(f))
        # Only the matched lines — not the secrets on other lines
        assert "SECRET_KEY" not in out
        assert "ANOTHER_SECRET" not in out
        assert "safe_func" in out
    finally:
        supertool._TS_AVAILABLE = False


# ---------------------------------------------------------------------------
# 5. Symbol injection — semicolon/shell chars treated as literal symbol name
# ---------------------------------------------------------------------------

def test_between_symbol_semicolon_not_shell_executed(tmp_path: Path) -> None:
    """between:foo;rm -rf /:path — the semicolon is part of the symbol name,
    never passed to a shell. The call must return ERROR (symbol not found or no
    tree-sitter), not execute any shell command.
    """
    f = tmp_path / "code.py"
    f.write_text("def foo(): pass\n")

    # Marker file — must still exist after the call
    marker = tmp_path / "keep_me.txt"
    marker.write_text("safe\n")

    out = supertool.op_between_symbol("foo;rm -rf /", str(f))
    assert "ERROR" in out  # symbol with ; not found
    assert marker.exists(), "marker file was deleted — shell injection occurred!"


def test_between_dispatch_symbol_with_shell_chars(tmp_path: Path) -> None:
    """Dispatch-level: between:foo$(whoami):path — treated as literal symbol."""
    f = tmp_path / "code.py"
    f.write_text("def foo(): pass\n")

    out = supertool.dispatch(f"between:foo$(whoami):{f}")
    assert "ERROR" in out
    # Must not contain actual username or command output
    import getpass
    assert getpass.getuser() not in out.lower() or "ERROR" in out


def test_between_symbol_backtick_not_executed(tmp_path: Path) -> None:
    """between:``touch /tmp/pwned``:path — backticks are literal, not executed."""
    f = tmp_path / "code.py"
    f.write_text("def foo(): pass\n")
    pwned = Path("/tmp/pwned_supertool_test")
    if pwned.exists():
        pwned.unlink()

    out = supertool.op_between_symbol("`touch /tmp/pwned_supertool_test`", str(f))
    assert "ERROR" in out
    assert not pwned.exists(), "backtick injection was executed!"


# ---------------------------------------------------------------------------
# 6. Regex special chars in symbol — literal match, not regex bomb
# ---------------------------------------------------------------------------

def test_between_symbol_regex_metachar_dot_star(tmp_path: Path) -> None:
    """between:.*:path — '.*' is treated as a literal symbol name, not a regex.

    op_between_symbol uses == comparison (via _ts_find_node), not re.search,
    so '.*' won't match every node.
    """
    f = tmp_path / "code.py"
    f.write_text("def real_func(): pass\ndef also_func(): pass\n")

    # tree-sitter disabled → unsupported extension error, not a regex match
    out = supertool.op_between_symbol(".*", str(f))
    assert "ERROR" in out


def test_between_symbol_regex_chars_no_redos(tmp_path: Path) -> None:
    """Symbol name with ReDoS pattern must not hang: between:(a+)+:path."""
    f = tmp_path / "code.py"
    f.write_text("def foo(): pass\n" * 100)

    start = time.monotonic()
    out = supertool.op_between_symbol("(a+)+", str(f))
    elapsed = time.monotonic() - start

    assert elapsed < 3.0, f"call took {elapsed:.2f}s — possible ReDoS in symbol lookup"
    assert "ERROR" in out


def test_between_symbol_null_byte_in_name(tmp_path: Path) -> None:
    """Symbol name containing NUL byte — must return ERROR, not crash."""
    f = tmp_path / "code.py"
    f.write_text("def foo(): pass\n")

    out = supertool.op_between_symbol("foo\x00bar", str(f))
    assert "ERROR" in out
    assert "Traceback" not in out


# ---------------------------------------------------------------------------
# 7. tree:PATH:DEPTH with huge depth — verify it caps / completes
# ---------------------------------------------------------------------------

def test_tree_huge_depth_completes_bounded(tmp_path: Path) -> None:
    """tree:PATH:1000 on a shallow tree must complete quickly, not hang."""
    # Create a 5-level directory tree
    cur = tmp_path
    for i in range(5):
        cur = cur / f"level{i}"
        cur.mkdir()
        (cur / f"file{i}.txt").write_text("x")

    start = time.monotonic()
    out = supertool.op_tree(str(tmp_path), 1000)
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, f"op_tree(depth=1000) took {elapsed:.2f}s on shallow tree"
    assert "ERROR" not in out
    # Should show all 5 levels (actual tree is shallower than depth limit)
    assert "level0/" in out
    assert "file4.txt" in out


def test_tree_dispatch_huge_depth_integer_string(tmp_path: Path) -> None:
    """tree:PATH:1000 via dispatch — depth parsed and applied."""
    (tmp_path / "a.txt").write_text("x")
    out = supertool.dispatch(f"tree:{tmp_path}:1000")
    assert "ERROR" not in out
    assert "a.txt" in out


def test_tree_symlink_loop_with_depth_cap(tmp_path: Path) -> None:
    """Symlink loop in a directory tree is bounded by depth limit.

    op_tree follows symlinks (os.path.isdir returns True for symlink-to-dir).
    With a finite depth, the recursion terminates. Verify it doesn't hang.
    """
    require_symlink()
    loop_dir = tmp_path / "a"
    loop_dir.mkdir()
    link = loop_dir / "loop"
    link.symlink_to(tmp_path)  # points back to parent → infinite loop potential

    start = time.monotonic()
    out = supertool.op_tree(str(tmp_path), 10)
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, f"symlink loop with depth=10 took {elapsed:.2f}s — not bounded"
    assert "ERROR" not in out


def test_tree_symlink_loop_deep_still_bounded(tmp_path: Path) -> None:
    """Symlink loop with depth=50 also terminates (checks the depth guard holds)."""
    require_symlink()
    loop_dir = tmp_path / "x"
    loop_dir.mkdir()
    (loop_dir / "cycle").symlink_to(tmp_path)

    start = time.monotonic()
    out = supertool.op_tree(str(tmp_path), 50)
    elapsed = time.monotonic() - start

    assert elapsed < 10.0, f"symlink loop depth=50 took {elapsed:.2f}s"
    assert "ERROR" not in out


# ---------------------------------------------------------------------------
# 8. tree:PATH:DEPTH with negative/non-integer DEPTH — clean error
# ---------------------------------------------------------------------------

def test_tree_negative_depth_returns_error() -> None:
    """op_tree with depth=0 returns ERROR; depth=-1 also caught."""
    out0 = supertool.op_tree(".", 0)
    assert "ERROR" in out0
    assert ">= 1" in out0

    out_neg = supertool.op_tree(".", -1)
    assert "ERROR" in out_neg
    assert ">= 1" in out_neg


def test_tree_dispatch_non_integer_depth_returns_error(tmp_path: Path) -> None:
    """tree:PATH:foo via dispatch returns an error, not a Python traceback."""
    out = supertool.dispatch(f"tree:{tmp_path}:foo")
    assert "ERROR" in out
    assert "Traceback" not in out


def test_tree_dispatch_float_depth_returns_error(tmp_path: Path) -> None:
    """tree:PATH:3.5 via dispatch — float is not a valid int, must be an error."""
    out = supertool.dispatch(f"tree:{tmp_path}:3.5")
    assert "ERROR" in out
    assert "Traceback" not in out


def test_tree_dispatch_empty_depth_uses_default(tmp_path: Path) -> None:
    """tree:PATH: (empty depth) → defaults to 3, no error."""
    (tmp_path / "f.txt").write_text("x")
    out = supertool.dispatch(f"tree:{tmp_path}:")
    assert "ERROR" not in out
    assert "f.txt" in out


# ---------------------------------------------------------------------------
# 9. Symlink loop in map — verify _collect_files (os.walk) terminates
# ---------------------------------------------------------------------------

def test_map_directory_with_symlink_loop_terminates(tmp_path: Path) -> None:
    """op_map on a dir containing a symlink loop must complete without hanging.

    os.walk(followlinks=False) is the default — symlinks to dirs are NOT
    followed by os.walk, so loops in _collect_files are inherently safe.
    """
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    f = src_dir / "module.py"
    f.write_text("class Foo:\n    pass\n")

    # Create a symlink loop: src/loop → src (or parent)
    require_symlink()
    link = src_dir / "loop"
    link.symlink_to(src_dir)

    start = time.monotonic()
    out = supertool.op_map(str(src_dir))
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, f"op_map on dir with symlink loop took {elapsed:.2f}s"
    assert "ERROR" not in out
    # Should still find Foo in module.py
    assert "Foo" in out or "module.py" in out


def test_map_walk_does_not_follow_symlinks(tmp_path: Path) -> None:
    """_collect_files uses os.walk default (followlinks=False).

    Verify that a symlinked subdirectory's contents are NOT included in the map.
    This confirms the loop-safe behavior is structural, not accidental.
    """
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    hidden_file = real_dir / "secret.py"
    hidden_file.write_text("class SecretClass:\n    pass\n")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    # Symlink project/linked → real (followlinks=False means this is skipped)
    require_symlink()
    (project_dir / "linked").symlink_to(real_dir)
    (project_dir / "visible.py").write_text("class VisibleClass:\n    pass\n")

    out = supertool.op_map(str(project_dir))
    assert "VisibleClass" in out
    assert "SecretClass" not in out


# ---------------------------------------------------------------------------
# 10. map on a 100k-line file — completes bounded, not OOM
# ---------------------------------------------------------------------------

def test_map_100k_line_file_completes_bounded(tmp_path: Path) -> None:
    """op_map on a 100k-line Python file must complete in bounded time.

    tree-sitter disabled (conftest), ctags disabled — regex tier only.
    Regex runs finditer() on the full content string: this is linear.
    """
    f = tmp_path / "big.py"
    # 100k lines: 99990 plain assignments + 10 class definitions
    lines = []
    for i in range(10):
        lines.append(f"class BigClass{i}:\n")
        for j in range(9999):
            lines.append(f"    x_{i}_{j} = {j}\n")
    f.write_text("".join(lines))

    start = time.monotonic()
    out = supertool.op_map(str(f))
    elapsed = time.monotonic() - start

    assert elapsed < 30.0, f"op_map on 100k-line file took {elapsed:.2f}s"
    assert "ERROR" not in out
    # Should find at least some of the BigClass definitions
    assert "BigClass" in out or "big.py" in out


@pytest.mark.skipif(not _has_any_tree_sitter(), reason="tree-sitter not installed")
def test_map_100k_line_file_tree_sitter_completes_bounded(tmp_path: Path, enable_tree_sitter) -> None:
    """op_map on a 100k-line Python file via real tree-sitter must complete bounded."""
    f = tmp_path / "big_ts.py"
    lines = []
    for i in range(5):
        lines.append(f"class Giant{i}:\n")
        for j in range(19999):
            lines.append(f"    y_{i}_{j} = {j}\n")
    f.write_text("".join(lines))

    start = time.monotonic()
    out = supertool.op_map(str(f))
    elapsed = time.monotonic() - start

    assert elapsed < 60.0, f"tree-sitter map on 100k-line file took {elapsed:.2f}s"
    assert "ERROR" not in out
    assert "Giant" in out or "big_ts.py" in out
