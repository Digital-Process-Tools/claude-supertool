from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# op_around
# ---------------------------------------------------------------------------

def test_around_finds_first_match(tmp_path: Path) -> None:
    lines = [f"line{i}" for i in range(1, 21)]
    lines[9] = "TARGET"  # line 10
    f = tmp_path / "src.py"
    f.write_text("\n".join(lines) + "\n")
    out = supertool.op_around("TARGET", str(f), n=3)
    assert "match at line 10" in out
    assert "TARGET" in out
    # Should include 3 lines before and after
    assert "line7" in out
    assert "line13" in out
    # Should not include lines too far away
    assert "line6" not in out
    assert "line14" not in out


def test_around_uses_arrow_marker_on_match_line(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("a\nMATCH\nb\n")
    out = supertool.op_around("MATCH", str(f), n=1)
    # Match line gets → marker
    assert "     2→MATCH" in out
    # Context lines get space marker
    assert "     1 a" in out
    assert "     3 b" in out


def test_around_no_match(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("nothing here\n")
    out = supertool.op_around("XYZZY_NOMATCH", str(f))
    assert "no match" in out


def test_around_directory_fans_out_to_files_with_match(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("alpha\nNEEDLE\nbeta\n")
    (tmp_path / "b.py").write_text("nothing\nhere\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("hit NEEDLE again\n")

    out = supertool.op_around("NEEDLE", str(tmp_path), n=1)
    assert "matched 2 file(s)" in out
    assert "=== a.py ===" in out
    assert "=== sub/c.py" in out or "=== sub\\c.py" in out
    assert "b.py" not in out  # files without match are skipped
    assert "NEEDLE" in out


def test_around_directory_no_match(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("nothing\n")
    (tmp_path / "b.py").write_text("zilch\n")
    out = supertool.op_around("XYZZY_NOMATCH", str(tmp_path))
    assert "no match" in out
    assert "scanned 2 file(s)" in out


def test_around_directory_skips_heavy_dirs(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("NEEDLE here\n")
    skip = tmp_path / ".git"
    skip.mkdir()
    (skip / "config").write_text("NEEDLE in git\n")
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "x.js").write_text("NEEDLE in deps\n")

    out = supertool.op_around("NEEDLE", str(tmp_path), n=0)
    assert "matched 1 file(s)" in out
    assert "=== a.py ===" in out
    assert ".git" not in out
    assert "node_modules" not in out


def test_around_missing_file_returns_error(tmp_path: Path) -> None:
    out = supertool.op_around("pattern", str(tmp_path / "nope.py"))
    assert "ERROR: file not found" in out


def test_around_default_n_is_10(tmp_path: Path) -> None:
    lines = ["pad"] * 15 + ["MATCH"] + ["pad"] * 15
    f = tmp_path / "src.py"
    f.write_text("\n".join(lines) + "\n")
    out = supertool.op_around("MATCH", str(f))
    # Default n=10: match at line 16, should show lines 6–26
    assert "showing lines 6" in out


def test_around_first_match_only(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("MATCH\nother\nMATCH\n")
    out = supertool.op_around("MATCH", str(f), n=0)
    # Only first match reported
    assert "match at line 1" in out


def test_around_empty_pattern_errors(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("content\n")
    out = supertool.op_around("", str(f))
    assert "ERROR: empty pattern" in out
