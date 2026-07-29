"""Tests for op_edit and op_replace_lines — single-file mutation primitives."""
from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# op_edit — exact-string, must-be-unique
# ---------------------------------------------------------------------------

def test_edit_replaces_unique_match(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a = 1\nb = 2\nc = 3\n")
    out = supertool.op_edit("b = 2", "b = 99", str(f))
    assert "edited" in out
    assert "b = 99" in f.read_text(encoding="utf-8")
    assert "b = 2" not in f.read_text(encoding="utf-8")


def test_edit_zero_matches_returns_error(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    out = supertool.op_edit("nope", "yep", str(f))
    assert "ERROR" in out
    assert "not found" in out


def test_edit_multiple_matches_returns_error(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("x = 1\nx = 2\nx = 3\n")
    out = supertool.op_edit("x =", "y =", str(f))
    assert "ERROR" in out
    assert "3 times" in out
    assert "ambiguous" in out
    # File untouched
    assert f.read_text(encoding="utf-8") == "x = 1\nx = 2\nx = 3\n"


def test_edit_missing_file_returns_error(tmp_path: Path) -> None:
    out = supertool.op_edit("a", "b", str(tmp_path / "nope.py"))
    assert "ERROR: file not found" in out


def test_edit_identical_strings_rejected(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    out = supertool.op_edit("a", "a", str(f))
    assert "identical" in out


def test_edit_empty_old_rejected(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    out = supertool.op_edit("", "x", str(f))
    assert "empty old" in out


def test_edit_receipt_shows_context(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    out = supertool.op_edit("line5", "LINE_FIVE", str(f))
    assert "LINE_FIVE" in out
    assert "line3" in out  # ±2 context
    assert "line7" in out


def test_edit_multiline_replacement(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\nMARKER\nz\n")
    out = supertool.op_edit("MARKER", "x1\nx2\nx3", str(f))
    assert "edited" in out
    assert f.read_text(encoding="utf-8") == "a\nx1\nx2\nx3\nz\n"


# ---------------------------------------------------------------------------
# op_replace_lines — line-range insert/replace/delete
# ---------------------------------------------------------------------------

def _write(tmp_path: Path, n: int) -> Path:
    f = tmp_path / "f.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, n + 1)) + "\n")
    return f


def test_replace_lines_swaps_range(tmp_path: Path) -> None:
    f = _write(tmp_path, 10)
    out = supertool.op_replace_lines(str(f), 3, 5, "X\nY")
    assert "replaced lines 3-5" in out
    text = f.read_text(encoding="utf-8")
    assert text == "line1\nline2\nX\nY\nline6\nline7\nline8\nline9\nline10\n"


def test_replace_lines_insert_only(tmp_path: Path) -> None:
    """end < start → pure insert before line `start`."""
    f = _write(tmp_path, 5)
    out = supertool.op_replace_lines(str(f), 3, 2, "NEW")
    assert "inserted 1 lines before line 3" in out
    assert f.read_text(encoding="utf-8") == "line1\nline2\nNEW\nline3\nline4\nline5\n"


def test_replace_lines_delete(tmp_path: Path) -> None:
    f = _write(tmp_path, 5)
    out = supertool.op_replace_lines(str(f), 2, 4, "")
    assert "deleted lines 2-4" in out
    assert f.read_text(encoding="utf-8") == "line1\nline5\n"


def test_replace_lines_end_beyond_total_errors(tmp_path: Path) -> None:
    f = _write(tmp_path, 3)
    out = supertool.op_replace_lines(str(f), 1, 99, "X")
    assert "ERROR" in out
    assert "end (99)" in out


def test_replace_lines_start_beyond_total_plus_one_errors(tmp_path: Path) -> None:
    f = _write(tmp_path, 3)
    out = supertool.op_replace_lines(str(f), 99, 100, "X")
    assert "ERROR" in out


def test_replace_lines_append_at_end(tmp_path: Path) -> None:
    """Insert at line total+1 → append."""
    f = _write(tmp_path, 3)
    out = supertool.op_replace_lines(str(f), 4, 3, "tail")
    assert "inserted 1 lines before line 4" in out
    assert f.read_text(encoding="utf-8") == "line1\nline2\nline3\ntail\n"


def test_replace_lines_invalid_start_errors(tmp_path: Path) -> None:
    f = _write(tmp_path, 3)
    out = supertool.op_replace_lines(str(f), 0, 1, "X")
    assert "ERROR" in out
    assert "start (0)" in out


def test_replace_lines_missing_file_errors(tmp_path: Path) -> None:
    out = supertool.op_replace_lines(str(tmp_path / "nope"), 1, 1, "X")
    assert "ERROR: file not found" in out


def test_replace_lines_receipt_shows_new_line_numbers(tmp_path: Path) -> None:
    f = _write(tmp_path, 10)
    out = supertool.op_replace_lines(str(f), 5, 5, "A\nB\nC")
    # 1 line replaced with 3 → new lines 5-7
    assert "5-7" in out
    assert "Δ +2" in out


# ---------------------------------------------------------------------------
# dispatch — argv → ops
# ---------------------------------------------------------------------------

def test_dispatch_edit(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo\nbar\n")
    out = supertool.dispatch(f"edit:foo:FOO:{f}")
    assert "edited" in out
    assert f.read_text(encoding="utf-8") == "FOO\nbar\n"


def test_dispatch_replace_lines(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\n")
    out = supertool.dispatch(f"replace_lines:{f}:2:2:B")
    assert "replaced lines 2-2" in out
    assert f.read_text(encoding="utf-8") == "a\nB\nc\n"


def test_dispatch_replace_lines_content_with_colons(tmp_path: Path) -> None:
    """CONTENT may contain colons — dispatch must rejoin parts past the index args."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\n")
    out = supertool.dispatch(f"replace_lines:{f}:2:2:url:http://x")
    assert "replaced" in out
    assert f.read_text(encoding="utf-8") == "a\nurl:http://x\nc\n"


# ---------------------------------------------------------------------------
# ::: dispatch — triple-colon for content with arbitrary `:`
# ---------------------------------------------------------------------------

def test_dispatch_edit_triple_colon(tmp_path: Path) -> None:
    """Triple-colon mode lets OLD/NEW contain `:` freely."""
    f = tmp_path / "x.php"
    f.write_text("Foo::bar();\necho 'hi';\n")
    out = supertool.dispatch(f"edit:::Foo::bar();:::Bar::baz();:::{f}")
    assert "edited" in out
    assert f.read_text(encoding="utf-8") == "Bar::baz();\necho 'hi';\n"


def test_dispatch_replace_lines_triple_colon_with_colons(tmp_path: Path) -> None:
    f = tmp_path / "x.php"
    f.write_text("a\nb\nc\n")
    out = supertool.dispatch(
        f"replace_lines:::{f}:::2:::2:::Class::method('http://x'):"
    )
    assert "replaced" in out
    assert "Class::method" in f.read_text(encoding="utf-8")


def test_dispatch_edit_triple_colon_multiline(tmp_path: Path) -> None:
    """OLD/NEW can span multiple lines under :::."""
    f = tmp_path / "x.py"
    f.write_text("start\nold1\nold2\nend\n")
    out = supertool.dispatch(f"edit:::old1\nold2:::new1\nnew2\nnew3:::{f}")
    assert "edited" in out
    assert f.read_text(encoding="utf-8") == "start\nnew1\nnew2\nnew3\nend\n"


def test_dispatch_single_colon_still_works_for_simple_content(tmp_path: Path) -> None:
    """Existing single-colon ops keep working — no breaking change."""
    f = tmp_path / "x.py"
    f.write_text("foo\nbar\n")
    out = supertool.dispatch(f"edit:foo:FOO:{f}")
    assert "edited" in out
    assert f.read_text(encoding="utf-8") == "FOO\nbar\n"


def test_dispatch_read_no_exclude_suffix_unaffected(tmp_path: Path) -> None:
    """`:::no-exclude` suffix must not be confused with triple-colon op mode."""
    (tmp_path / "x.py").write_text("hit\n")
    # `grep` op with single-colon args + :::no-exclude suffix — should still parse
    out = supertool.dispatch(f"grep:hit:{tmp_path}:5:::no-exclude")
    assert "1 results" in out


def test_dispatch_read_grep_filter_unaffected(tmp_path: Path) -> None:
    """`read:PATH:::grep=PAT` — `:::` mid-arg, not as op separator."""
    f = tmp_path / "x.py"
    f.write_text("foo\nbar\nFoo\n")
    out = supertool.dispatch(f"read:{f}:::grep=foo")
    assert "1→foo" in out
    assert "bar" not in out


# ---------------------------------------------------------------------------
# Crash-safety: atomic write
# ---------------------------------------------------------------------------

def test_edit_atomic_write_no_temp_files_left(tmp_path: Path) -> None:
    """After a successful edit, no .supertool-*.tmp files remain."""
    f = tmp_path / "x.py"
    f.write_text("foo\n")
    supertool.op_edit("foo", "FOO", str(f))
    leftovers = list(tmp_path.glob(".supertool-*"))
    assert leftovers == []


def test_replace_lines_atomic_write_no_temp_files_left(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\n")
    supertool.op_replace_lines(str(f), 2, 2, "B")
    leftovers = list(tmp_path.glob(".supertool-*"))
    assert leftovers == []


def test_edit_failed_write_preserves_original(tmp_path: Path, monkeypatch) -> None:
    """If os.replace fails mid-write, original content is preserved."""
    f = tmp_path / "x.py"
    f.write_text("original\n")

    def boom(*a, **kw):
        raise OSError("simulated crash")

    monkeypatch.setattr(supertool.os, "replace", boom)
    out = supertool.op_edit("original", "NEW", str(f))
    assert "ERROR" in out
    # File still has original content — atomic write means no partial state
    assert f.read_text(encoding="utf-8") == "original\n"
    # Temp file cleaned up
    assert list(tmp_path.glob(".supertool-*")) == []


# ---------------------------------------------------------------------------
# Validation guards
# ---------------------------------------------------------------------------

def test_replace_atomic_write_no_temp_files_left(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("foo bar foo\n")
    supertool.op_replace("foo", "FOO", str(f))
    leftovers = list(tmp_path.glob(".supertool-*"))
    assert leftovers == []
    assert "FOO" in f.read_text(encoding="utf-8")


def test_replace_lines_negative_end_rejected(tmp_path: Path) -> None:
    f = _write(tmp_path, 5)
    out = supertool.op_replace_lines(str(f), 3, -1, "X")
    assert "ERROR" in out
    assert "end (-1)" in out
    # File untouched
    assert f.read_text(encoding="utf-8") == "line1\nline2\nline3\nline4\nline5\n"


# ---------------------------------------------------------------------------
# _decode_escapes — CLI escape sequences for mutating ops
# ---------------------------------------------------------------------------

def test_decode_escapes_newline_tab_return() -> None:
    assert supertool._decode_escapes("a\\nb") == "a\nb"
    assert supertool._decode_escapes("a\\tb") == "a\tb"
    assert supertool._decode_escapes("a\\rb") == "a\rb"


def test_decode_escapes_double_backslash_protects_n() -> None:
    """`\\\\n` (literal backslash + n) must NOT decode to newline."""
    assert supertool._decode_escapes("a\\\\nb") == "a\\nb"


def test_decode_escapes_no_backslash_passthrough() -> None:
    assert supertool._decode_escapes("plain text") == "plain text"


def test_dispatch_edit_decodes_newline_in_new(tmp_path: Path) -> None:
    """`./supertool 'edit:::OLD:::a\\nb:::PATH'` must write a real newline."""
    f = tmp_path / "x.txt"
    f.write_text("MARKER\n")
    out = supertool.dispatch(f"edit:::MARKER:::a\\nb:::{f}")
    assert "edited" in out
    assert f.read_text(encoding="utf-8") == "a\nb\n"


def test_dispatch_replace_decodes_newline_in_new(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("foo\n")
    out = supertool.dispatch(f"replace:::foo:::line1\\nline2:::{f}")
    assert "foo" not in f.read_text(encoding="utf-8")
    assert f.read_text(encoding="utf-8") == "line1\nline2\n"


def test_dispatch_edit_decodes_tab(tmp_path: Path) -> None:
    """`\\t` in NEW writes a real tab byte."""
    f = tmp_path / "x.txt"
    f.write_text("MARKER\n")
    supertool.dispatch(f"edit:::MARKER:::col1\\tcol2:::{f}")
    assert f.read_text(encoding="utf-8") == "col1\tcol2\n"


def test_dispatch_replace_lines_decodes_newline(tmp_path: Path) -> None:
    """replace_lines CONTENT also goes through _decode_escapes."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\n")
    # Replace line 2 (1-indexed) with two lines via \n
    supertool.dispatch(f"replace_lines:::{f}:::2:::2:::x1\\nx2")
    assert f.read_text(encoding="utf-8") == "a\nx1\nx2\nc\n"


def test_dispatch_edit_double_backslash_stays_literal(tmp_path: Path) -> None:
    r"""`\\n` (literal backslash + n) must NOT decode to newline."""
    f = tmp_path / "x.txt"
    f.write_text("MARKER\n")
    # Pass 4 chars `\\n` → expected output: 2 chars `\n` (literal)
    supertool.dispatch(f"edit:::MARKER:::a\\\\nb:::{f}")
    assert f.read_text(encoding="utf-8") == "a\\nb\n"
