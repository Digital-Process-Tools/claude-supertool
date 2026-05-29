from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# op_wc
# ---------------------------------------------------------------------------

def test_wc_counts(tmp_path: Path) -> None:
    f = tmp_path / "sample.txt"
    f.write_text("hello world\nfoo bar baz\n")
    out = supertool.op_wc(str(f))
    assert "2 " in out  # 2 newlines
    assert " 5 " in out  # 5 words
    assert str(f) in out


def test_wc_missing_file() -> None:
    out = supertool.op_wc("/nonexistent/file.txt")
    assert "ERROR" in out


def test_wc_empty_path() -> None:
    out = supertool.op_wc("")
    assert "ERROR" in out


def test_wc_directory(tmp_path: Path) -> None:
    out = supertool.op_wc(str(tmp_path))
    assert "ERROR" in out


def test_wc_flags_minified_single_line(tmp_path: Path) -> None:
    f = tmp_path / "bundle.min.js"
    f.write_text("a" * 25000)  # overflows MAX_READ_BYTES (20000)
    out = supertool.op_wc(str(f))
    assert "[minified" in out
    assert "25000 chars" in out


def test_wc_flags_minified_behind_leading_comment(tmp_path: Path) -> None:
    # Same #240 shape head/tail must agree on: short comment + giant line.
    f = tmp_path / "lib.min.js"
    f.write_text("/* c */\n" + "b" * 25000)
    out = supertool.op_wc(str(f))
    assert "[minified" in out


def test_wc_normal_large_file_not_flagged(tmp_path: Path) -> None:
    f = tmp_path / "big.log"
    f.write_text("\n".join(f"line{i}" for i in range(5000)))
    out = supertool.op_wc(str(f))
    assert "minified" not in out
