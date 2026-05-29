from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# op_tail / op_head
# ---------------------------------------------------------------------------

def test_tail_returns_last_n_lines(tmp_path: Path) -> None:
    f = tmp_path / "log.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    out = supertool.op_tail(str(f), 3)
    assert "     8→line8" in out
    assert "     9→line9" in out
    assert "    10→line10" in out
    assert "line7" not in out


def test_head_returns_first_n_lines(tmp_path: Path) -> None:
    f = tmp_path / "log.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    out = supertool.op_head(str(f), 3)
    assert "     1→line1" in out
    assert "     2→line2" in out
    assert "     3→line3" in out
    assert "line4" not in out


def test_tail_missing_file(tmp_path: Path) -> None:
    out = supertool.op_tail(str(tmp_path / "nope.txt"), 5)
    assert "ERROR: file not found" in out


def test_head_missing_file(tmp_path: Path) -> None:
    out = supertool.op_head(str(tmp_path / "nope.txt"), 5)
    assert "ERROR: file not found" in out


def test_tail_file_shorter_than_n(tmp_path: Path) -> None:
    f = tmp_path / "short.txt"
    f.write_text("only\none\n")
    out = supertool.op_tail(str(f), 100)
    assert "     1→only" in out
    assert "     2→one" in out


def test_head_file_shorter_than_n(tmp_path: Path) -> None:
    f = tmp_path / "short.txt"
    f.write_text("only\none\n")
    out = supertool.op_head(str(f), 100)
    assert "showing first 2" in out


# ---------------------------------------------------------------------------
# minified single-line files — char window instead of one giant line (#240)
# ---------------------------------------------------------------------------

def test_head_minified_single_line(tmp_path: Path) -> None:
    f = tmp_path / "bundle.min.js"
    f.write_text("x" * 25000)  # overflows MAX_READ_BYTES (20000)
    out = supertool.op_head(str(f), 20)
    assert "minified" in out
    assert "25000 chars" in out
    assert "showing first 1000" in out  # default CHAR_WINDOW_CHARS
    assert "24000 more chars truncated" in out
    assert len(out) < 5000  # the giant line is NOT dumped in full


def test_tail_minified_single_line(tmp_path: Path) -> None:
    f = tmp_path / "bundle.min.js"
    f.write_text("x" * 25000)
    out = supertool.op_tail(str(f), 20)
    assert "minified" in out
    assert "showing last 1000" in out
    assert "24000 clipped" in out
    assert "24000 earlier chars" in out  # symmetric leading marker
    assert len(out) < 5000


def test_head_minified_behind_leading_comment(tmp_path: Path) -> None:
    # The #240 regression the old detector missed: a short comment line, then
    # a giant minified line. "no newline in first chunk" returned False here,
    # so head dumped the whole 25KB line.
    f = tmp_path / "lib.min.js"
    f.write_text("/* license */\n" + "y" * 25000)
    out = supertool.op_head(str(f), 20)
    assert "minified" in out
    assert len(out) < 5000


def test_head_large_normal_file_not_minified(tmp_path: Path) -> None:
    # Overflows the cap but is genuinely line-based — must NOT char-window.
    f = tmp_path / "big.log"
    f.write_text("\n".join(f"line{i}" for i in range(5000)))
    out = supertool.op_head(str(f), 3)
    assert "minified" not in out
    assert "     1→line0" in out
    assert "     3→line2" in out


def test_head_minified_respects_char_window_env(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SUPERTOOL_HEAD_CHAR_WINDOW", "50")
    f = tmp_path / "bundle.min.js"
    f.write_text("z" * 25000)
    out = supertool.op_head(str(f), 20)
    assert "showing first 50" in out
    assert "24950 more chars truncated" in out
