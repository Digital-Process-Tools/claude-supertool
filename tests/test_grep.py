from __future__ import annotations

from pathlib import Path

import supertool


def test_grep_finds_match_in_single_file(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("class Foo:\n    pass\n\nclass Bar:\n    pass\n")
    out = supertool.op_grep("class", str(f))
    assert "(2 results" in out
    assert "src.py:1:class Foo:" in out
    assert "src.py:4:class Bar:" in out


def test_grep_no_match_returns_zero(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("class Foo:\n")
    out = supertool.op_grep("NOTHINGMATCHES_XYZZY", str(f))
    assert "(0 results" in out


def test_grep_empty_pattern_errors() -> None:
    out = supertool.op_grep("", "/tmp")
    assert "ERROR: empty pattern" in out


def test_grep_respects_limit(tmp_path: Path) -> None:
    f = tmp_path / "many.py"
    content = "\n".join(f"match line {i}" for i in range(1, 20)) + "\n"
    f.write_text(content)
    out = supertool.op_grep("match", str(f), limit=3)
    assert "limit 3" in out
    # Count actual result lines (path:lineno:content format)
    result_lines = [ln for ln in out.split("\n") if ":" in ln and "match line" in ln]
    assert len(result_lines) == 3


def test_grep_on_directory_filters_by_extension(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("needle = 1\n")
    (tmp_path / "doc.md").write_text("needle in docs\n")
    (tmp_path / "log.log").write_text("needle in log\n")  # should be skipped
    out = supertool.op_grep("needle", str(tmp_path), limit=10)
    assert "code.py" in out
    assert "doc.md" in out
    assert "log.log" not in out


# --- Auto-read on grep (small single file + match) ---

def test_grep_auto_reads_small_single_file_on_match(tmp_path: Path) -> None:
    f = tmp_path / "small.py"
    f.write_text("found_it = True\n")
    out = supertool.op_grep("found_it", str(f))
    assert "[auto-read:" in out
    assert "(1 lines" in out  # The render_file output
    assert "     1→found_it = True" in out


def test_grep_no_auto_read_when_no_match(tmp_path: Path) -> None:
    f = tmp_path / "small.py"
    f.write_text("nothing_here = True\n")
    out = supertool.op_grep("XXX_NO_MATCH", str(f))
    assert "[auto-read:" not in out


def test_grep_no_auto_read_on_directory(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("foo = 1\n")
    out = supertool.op_grep("foo", str(tmp_path))
    # Matched in file a.py, but path is the directory
    assert "[auto-read:" not in out


def test_grep_no_auto_read_on_large_file(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "big.py"
    f.write_text("needle\n" + "x" * 30000)
    out = supertool.op_grep("needle", str(f))
    assert "[auto-read:" not in out


# ---------------------------------------------------------------------------
# op_grep with context lines
# ---------------------------------------------------------------------------

def test_grep_context_zero_same_as_no_context(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("class Foo:\n    pass\n\nclass Bar:\n    pass\n")
    out_plain = supertool.op_grep("class", str(f), limit=10, context=0)
    out_ctx = supertool.op_grep("class", str(f), limit=10)
    assert out_plain == out_ctx


def test_grep_context_includes_surrounding_lines(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    # Match at line 5, 2 lines of context → lines 3-7 shown; lines 1-2 and 8-10 excluded
    f.write_text("skip1\nskip2\nctx_before2\nctx_before1\nMATCH\nctx_after1\nctx_after2\nskip3\nskip4\n")
    out = supertool.op_grep("MATCH", str(f), limit=10, context=2)
    # Match line uses colon separator
    assert f"{f}:5:MATCH" in out
    # Context lines use dash separator
    assert f"{f}-4-ctx_before1" in out
    assert f"{f}-6-ctx_after1" in out
    assert f"{f}-3-ctx_before2" in out
    assert f"{f}-7-ctx_after2" in out
    # Lines beyond context are not included
    assert "skip1" not in out
    assert "skip2" not in out
    assert "skip3" not in out


def test_grep_context_header_shows_context_value(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("MATCH\n")
    out = supertool.op_grep("MATCH", str(f), limit=10, context=3)
    assert "context 3" in out


def test_grep_context_separator_between_nonadjacent_groups(tmp_path: Path) -> None:
    lines = [f"line{i}" for i in range(1, 21)]
    lines[3] = "MATCH_A"   # line 4
    lines[16] = "MATCH_B"  # line 17
    f = tmp_path / "src.py"
    f.write_text("\n".join(lines) + "\n")
    out = supertool.op_grep("MATCH", str(f), limit=10, context=1)
    # Groups should be separated by --
    assert "--\n" in out
    assert "MATCH_A" in out
    assert "MATCH_B" in out


def test_grep_context_no_separator_for_adjacent_matches(tmp_path: Path) -> None:
    lines = ["before", "MATCH_A", "MATCH_B", "after"]
    f = tmp_path / "src.py"
    f.write_text("\n".join(lines) + "\n")
    out = supertool.op_grep("MATCH", str(f), limit=10, context=1)
    # Adjacent matches → merged group → no -- separator
    assert "--\n" not in out
    assert "MATCH_A" in out
    assert "MATCH_B" in out


def test_grep_context_overlapping_windows_merge(tmp_path: Path) -> None:
    # Two matches close enough that context windows overlap
    lines = ["a", "MATCH_A", "b", "MATCH_B", "c"]
    f = tmp_path / "src.py"
    f.write_text("\n".join(lines) + "\n")
    out = supertool.op_grep("MATCH", str(f), limit=10, context=2)
    # With context=2: window A covers lines 1-3, window B covers lines 2-5
    # They overlap → one group, no --
    assert "--\n" not in out
    assert "MATCH_A" in out
    assert "MATCH_B" in out


def test_grep_context_clamps_to_file_boundaries(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("MATCH\nline2\nline3\n")
    # Match at line 1 with context=5 — should not go negative
    out = supertool.op_grep("MATCH", str(f), limit=10, context=5)
    assert "MATCH" in out
    assert "ERROR" not in out


def test_grep_context_no_auto_read(tmp_path: Path) -> None:
    f = tmp_path / "small.py"
    f.write_text("MATCH\n")
    out = supertool.op_grep("MATCH", str(f), limit=10, context=1)
    # Auto-read should be skipped when context is active
    assert "[auto-read:" not in out


# ---------------------------------------------------------------------------
# grep count mode
# ---------------------------------------------------------------------------

def test_grep_count_single_file(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("import os\nimport sys\ndef main():\n    pass\n")
    out = supertool.op_grep("import", str(f), count_only=True)
    assert "2 total matches across 1 files" in out
    assert f"{f}:2" in out


def test_grep_count_multiple_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("foo\nbar\n")
    (tmp_path / "b.py").write_text("foo\nfoo\n")
    out = supertool.op_grep("foo", str(tmp_path), count_only=True)
    assert "3 total matches across 2 files" in out


def test_grep_count_no_matches(tmp_path: Path) -> None:
    f = tmp_path / "empty.py"
    f.write_text("nothing here\n")
    out = supertool.op_grep("ZZZZZZ", str(f), count_only=True)
    assert "0 total matches across 0 files" in out


def test_grep_count_empty_pattern() -> None:
    out = supertool.op_grep("", ".", count_only=True)
    assert "ERROR" in out


# ---------------------------------------------------------------------------
# _grep_recursive — additional coverage
# ---------------------------------------------------------------------------

def test_grep_recursive_invalid_regex(tmp_path: Path) -> None:
    """_grep_recursive falls back to escaped pattern on invalid regex."""
    f = tmp_path / "test.php"
    f.write_text("array[0]\narray[1]\nnope\n")
    results = supertool._grep_recursive("[invalid", str(f), 10)
    # Should not crash — falls back to literal match
    assert isinstance(results, list)


def test_grep_recursive_unreadable_file(tmp_path: Path) -> None:
    """_grep_recursive skips unreadable files."""
    f = tmp_path / "secret.php"
    f.write_text("class Foo {}\n")
    f.chmod(0o000)
    try:
        results = supertool._grep_recursive("class", str(f), 10)
        assert results == []
    finally:
        f.chmod(0o644)


def test_grep_recursive_respects_limit(tmp_path: Path) -> None:
    """_grep_recursive stops at limit."""
    f = tmp_path / "many.php"
    f.write_text("match\n" * 20)
    results = supertool._grep_recursive("match", str(f), 3)
    assert len(results) == 3


def test_grep_recursive_limit_across_files(tmp_path: Path) -> None:
    """_grep_recursive stops scanning files after limit reached."""
    for i in range(5):
        (tmp_path / f"f{i}.php").write_text("match\nmatch\nmatch\n")
    results = supertool._grep_recursive("match", str(tmp_path), 4)
    assert len(results) == 4


# ---------------------------------------------------------------------------
# _grep_count — additional coverage
# ---------------------------------------------------------------------------

def test_grep_count_invalid_regex(tmp_path: Path) -> None:
    """_grep_count falls back to escaped pattern on invalid regex."""
    f = tmp_path / "test.php"
    f.write_text("array[0]\narray[1]\nnope\n")
    counts = supertool._grep_count("[invalid", str(f), 10)
    # Should match literally as escaped pattern — "[invalid" won't match anything
    # but the function should not crash
    assert isinstance(counts, dict)


def test_grep_count_unreadable_file(tmp_path: Path) -> None:
    """_grep_count skips unreadable files."""
    f = tmp_path / "secret.php"
    f.write_text("class Foo {}\n")
    f.chmod(0o000)
    try:
        counts = supertool._grep_count("class", str(f), 10)
        assert isinstance(counts, dict)
    finally:
        f.chmod(0o644)


# ---------------------------------------------------------------------------
# _grep_recursive_context — additional coverage
# ---------------------------------------------------------------------------

def test_grep_recursive_context_invalid_regex(tmp_path: Path) -> None:
    """_grep_recursive_context falls back to escaped pattern on invalid regex."""
    f = tmp_path / "test.php"
    f.write_text("array[0]\narray[1]\nnope\n")
    groups = supertool._grep_recursive_context("[invalid", str(f), 10, 1)
    assert isinstance(groups, list)


def test_grep_recursive_context_unreadable_file(tmp_path: Path) -> None:
    """_grep_recursive_context skips unreadable files."""
    f = tmp_path / "secret.php"
    f.write_text("class Foo {}\n")
    f.chmod(0o000)
    try:
        groups = supertool._grep_recursive_context("class", str(f), 10, 1)
        assert groups == []
    finally:
        f.chmod(0o644)


def test_grep_recursive_context_no_match(tmp_path: Path) -> None:
    """_grep_recursive_context returns empty for no matches."""
    f = tmp_path / "test.php"
    f.write_text("nothing here\njust text\n")
    groups = supertool._grep_recursive_context("zzz_no_match", str(f), 10, 1)
    assert groups == []


def test_grep_recursive_context_respects_limit(tmp_path: Path) -> None:
    """_grep_recursive_context stops collecting after limit matches."""
    f = tmp_path / "many.php"
    # Spread matches so they form separate groups
    lines = []
    for i in range(20):
        if i % 5 == 0:
            lines.append("MATCH_LINE")
        else:
            lines.append("filler")
    f.write_text("\n".join(lines) + "\n")
    groups = supertool._grep_recursive_context("MATCH_LINE", str(f), 2, 1)
    match_count = sum(1 for g in groups for line in g if line[2] == "match")
    assert match_count == 2


def test_grep_recursive_context_limit_across_files(tmp_path: Path) -> None:
    """_grep_recursive_context stops scanning files after limit reached."""
    for i in range(5):
        (tmp_path / f"f{i}.php").write_text("MATCH\nfiller\nMATCH\n")
    groups = supertool._grep_recursive_context("MATCH", str(tmp_path), 3, 0)
    match_count = sum(1 for g in groups for line in g if line[2] == "match")
    assert match_count == 3
