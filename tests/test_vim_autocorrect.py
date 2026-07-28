"""Autocorrect + leniency tests for op_vim script parsing.

These cover three adaptations Kevin kept tripping on:
1. `:r -` reads stdin (was documented but unimplemented)
2. `d5d` / `c5w` / `y5y` — count in the middle → autocorrect to prefix form
3. `\\n` (escape) outside TEXT — autocorrect to action separator
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# 1. :r - reads stdin
# ---------------------------------------------------------------------------

def test_r_dash_reads_stdin(tmp_path: Path, monkeypatch) -> None:
    """`:r -` reads from stdin and inserts after current line, like `:r FILE`."""
    f = tmp_path / "x.md"
    f.write_text("# heading\n")
    monkeypatch.setattr("sys.stdin", io.StringIO("body line 1\nbody line 2\n"))
    out = supertool.op_vim(str(f), "G␞:r -")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "# heading\nbody line 1\nbody line 2\n"


def test_r_dash_empty_stdin_inserts_nothing(tmp_path: Path, monkeypatch) -> None:
    """`:r -` with empty stdin is a no-op (file unchanged except trailing nl normalize)."""
    f = tmp_path / "x.md"
    f.write_text("only line\n")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    out = supertool.op_vim(str(f), "G␞:r -")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "only line\n"


# ---------------------------------------------------------------------------
# 2. Count-in-middle autocorrect (d5d → 5dd)
# ---------------------------------------------------------------------------

def test_d5d_autocorrects_to_5dd(tmp_path: Path) -> None:
    """`d5d` is unambiguously `5dd` — count goes before verb. Autocorrect, don't error."""
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\nd\ne\nf\n")
    out = supertool.op_vim(str(f), "d5d")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "f\n"  # first 5 lines deleted


def test_c5w_autocorrects_to_5cw(tmp_path: Path) -> None:
    """`c5w` → `5cw` — autocorrect rewrites the action so it doesn't trip
    'unknown verb'. (Whether the impl actually honors the 5 count for `cw`
    is a separate concern — this test only proves the rewrite happens.)"""
    f = tmp_path / "x.py"
    f.write_text("one two three four five rest\n")
    out = supertool.op_vim(str(f), "c5wX")
    assert "ERROR" not in out, out  # no 'unknown verb' = autocorrect ran


def test_y5y_autocorrects_to_5yy(tmp_path: Path) -> None:
    """`y5y` → `5yy`. Yank 5 lines."""
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\nd\ne\n")
    out = supertool.op_vim(str(f), "y5y␞G␞p")
    assert "ERROR" not in out, out
    # yanked 5 lines, paste after last (cursor on 'e' after G)
    assert f.read_text(encoding="utf-8").count("a\n") == 2  # one original, one pasted


# ---------------------------------------------------------------------------
# 3. \n escape outside TEXT → action separator
# ---------------------------------------------------------------------------

def test_backslash_n_after_search_stays_literal_for_regex(tmp_path: Path) -> None:
    """`\\n` inside a search pattern stays literal — regex can legitimately
    use `\\n` for multiline matching. Don't autocorrect that away. The
    canonical way to chain after search is `;` not `\\n`."""
    f = tmp_path / "x.py"
    f.write_text("alpha\nbeta\ngamma\n")
    # Regex `alpha\nbeta` matches across the real newline (MULTILINE re).
    out = supertool.op_vim(str(f), "/alpha\\nbeta␞dd")
    assert "ERROR" not in out, out
    # `dd` deletes one line at the cursor's landing (start of match → "alpha").
    assert f.read_text(encoding="utf-8") == "beta\ngamma\n"


def test_forward_search_strips_trailing_slash_on_miss(tmp_path: Path) -> None:
    """`/PAT/` (trailing `/`, sed muscle memory) — strip and retry."""
    f = tmp_path / "x.py"
    f.write_text("alpha\nNullLogger\nbeta\n")
    out = supertool.op_vim(str(f), "/NullLogger/␞dd")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "alpha\nbeta\n"


def test_forward_search_keeps_literal_trailing_slash_when_found(tmp_path: Path) -> None:
    """Regression — if `PAT/` literally exists, don't strip and re-search.
    Stripping would land the cursor at the wrong offset (start of `PAT`
    instead of inside `PAT/`)."""
    f = tmp_path / "x.txt"
    f.write_text("alpha PAT/ beta\n")
    out = supertool.op_vim(str(f), "/PAT/␞ipoke ")
    assert "ERROR" not in out, out
    # cursor at start of "PAT/" (position 6), `i` inserts "poke " before it
    assert f.read_text(encoding="utf-8") == "alpha poke PAT/ beta\n"


def test_backward_search_strips_trailing_slash_on_miss(tmp_path: Path) -> None:
    """`?PAT/` (trailing `/`) — strip and retry, mirror of forward."""
    f = tmp_path / "x.py"
    f.write_text("alpha\nNullLogger\nbeta\n")
    out = supertool.op_vim(str(f), "G␞?NullLogger/␞dd")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "alpha\nbeta\n"


def test_backslash_n_inside_TEXT_still_decodes_to_newline(tmp_path: Path) -> None:
    """Regression — inside TEXT (i/a/o etc.), `\\n` still decodes to a real newline,
    NOT an action separator. The behavior is context-sensitive."""
    f = tmp_path / "x.py"
    f.write_text("end\n")
    out = supertool.op_vim(str(f), "ifoo\\nbar\\n")
    assert "ERROR" not in out, out
    assert f.read_text(encoding="utf-8") == "foo\nbar\nend\n"
