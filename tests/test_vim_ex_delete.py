"""Tests for vim ex-mode :d (delete) family.

Real vim supports rich `:d` variants:
- `:%d`           — delete all lines (whole buffer)
- `:Nd`           — delete line N
- `:N,Md`         — delete lines N through M (inclusive)
- `:.d`           — delete current line (cursor's line)
- `:$d`           — delete last line
- `:.,$d`         — delete from current to end
- `:g/PAT/d`      — delete all lines matching PAT
- `:v/PAT/d`      — delete lines NOT matching PAT (also :g!/PAT/d)
"""
from __future__ import annotations

from pathlib import Path

import supertool


def test_percent_d_empties_buffer(tmp_path: Path) -> None:
    """`:%d` deletes every line."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\ne\n")
    out = supertool.op_vim(str(f), ":%d")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == ""


def test_single_line_delete(tmp_path: Path) -> None:
    """`:3d` deletes line 3 only."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\ne\n")
    out = supertool.op_vim(str(f), ":3d")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "a\nb\nd\ne\n"


def test_range_delete(tmp_path: Path) -> None:
    """`:2,4d` deletes lines 2..4."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\ne\n")
    out = supertool.op_vim(str(f), ":2,4d")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "a\ne\n"


def test_dot_d_deletes_current_line(tmp_path: Path) -> None:
    """`:.d` deletes the cursor's line."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\ne\n")
    out = supertool.op_vim(str(f), "3G\x1e:.d")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "a\nb\nd\ne\n"


def test_dollar_d_deletes_last_line(tmp_path: Path) -> None:
    """`:$d` deletes the last line."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\n")
    out = supertool.op_vim(str(f), ":$d")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "a\nb\n"


def test_dot_to_dollar_d(tmp_path: Path) -> None:
    """`:.,$d` from cursor line through end."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\ne\n")
    out = supertool.op_vim(str(f), "3G\x1e:.,$d")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "a\nb\n"


def test_g_pattern_d_deletes_matching(tmp_path: Path) -> None:
    """`:g/foo/d` deletes every line containing foo."""
    f = tmp_path / "x.txt"
    f.write_text("foo 1\nbar\nfoo 2\nbaz\nfoo 3\n")
    out = supertool.op_vim(str(f), ":g/foo/d")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "bar\nbaz\n"


def test_v_pattern_d_deletes_non_matching(tmp_path: Path) -> None:
    """`:v/keep/d` deletes lines NOT containing 'keep'."""
    f = tmp_path / "x.txt"
    f.write_text("keep this\ntrash\nkeep that\nmore trash\nkeep one more\n")
    out = supertool.op_vim(str(f), ":v/keep/d")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "keep this\nkeep that\nkeep one more\n"


def test_g_bang_equivalent_to_v(tmp_path: Path) -> None:
    """`:g!/PAT/d` is the same as `:v/PAT/d`."""
    f = tmp_path / "x.txt"
    f.write_text("keep this\ntrash\nkeep that\n")
    out = supertool.op_vim(str(f), ":g!/keep/d")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "keep this\nkeep that\n"


def test_out_of_range_errors(tmp_path: Path) -> None:
    """`:99d` on a 3-line file is an error."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\n")
    out = supertool.op_vim(str(f), ":99d")
    assert "ERROR" in out


def test_inverted_range_errors(tmp_path: Path) -> None:
    """`:5,2d` is an error (start after end)."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\ne\n")
    out = supertool.op_vim(str(f), ":5,2d")
    assert "ERROR" in out


def test_mixed_range_n_to_dollar(tmp_path: Path) -> None:
    """`:2,$d` deletes from line 2 to last."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\n")
    out = supertool.op_vim(str(f), ":2,$d")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "a\n"


def test_mixed_range_dot_to_m(tmp_path: Path) -> None:
    """`:.,4d` deletes from cursor to line 4."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\ne\n")
    out = supertool.op_vim(str(f), "2G\x1e:.,4d")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "a\ne\n"


def test_g_pattern_no_match_errors(tmp_path: Path) -> None:
    """`:g/nope/d` when nothing matches is an error (consistent with :s)."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\n")
    out = supertool.op_vim(str(f), ":g/nope/d")
    assert "ERROR" in out
