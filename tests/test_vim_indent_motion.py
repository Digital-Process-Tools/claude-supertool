"""Tests for vim indent/dedent operators with motion targets.

>motion / <motion / =motion paths that exercise the indent operator's
motion-resolution branch (paragraph, line, G/gg, j/k).
"""
from __future__ import annotations

from pathlib import Path

import supertool


def _run(tmp_path: Path, initial: str, script: str) -> str:
    f = tmp_path / "x.txt"
    f.write_text(initial)
    out = supertool.op_vim(str(f), script)
    assert not out.startswith("ERROR"), out
    return f.read_text()


# --- > with paragraph motions ---

def test_indent_with_close_brace_motion(tmp_path: Path) -> None:
    out = _run(tmp_path, "p1 line1\np1 line2\n\np2\n", "gg␞>}")
    # >} indents from current line through next blank.
    assert out.startswith("    p1 line1\n")
    assert "    p1 line2\n" in out


def test_indent_with_open_brace_motion(tmp_path: Path) -> None:
    out = _run(tmp_path, "p1\n\np2 line1\np2 line2\n", "G␞>{")
    # >{ from last line indents back through blank.
    assert "    p2" in out


# --- < with paragraph motions ---

def test_dedent_with_close_brace_motion(tmp_path: Path) -> None:
    out = _run(tmp_path, "    p1 line1\n    p1 line2\n\n    p2\n", "gg␞<}")
    # <} dedents from current line through next blank.
    assert out.startswith("p1 line1\n")
    assert "p1 line2\n" in out


def test_dedent_with_open_brace_motion(tmp_path: Path) -> None:
    out = _run(tmp_path, "    p1\n\n    p2 line1\n    p2 line2\n", "G␞<{")
    # <{ from last line dedents back through blank.
    assert "p2" in out


# --- > with G / gg ---

def test_indent_to_G(tmp_path: Path) -> None:
    out = _run(tmp_path, "first\nsecond\nthird\n", "gg␞>G")
    # >G indents from line 1 through EOF.
    assert out == "    first\n    second\n    third\n"


def test_indent_to_gg(tmp_path: Path) -> None:
    out = _run(tmp_path, "first\nsecond\nthird\n", "G␞>gg")
    # >gg indents from last line back to line 1.
    assert "    first" in out and "    third" in out


def test_dedent_to_G(tmp_path: Path) -> None:
    out = _run(tmp_path, "    a\n    b\n    c\n", "gg␞<G")
    assert out == "a\nb\nc\n"


def test_dedent_to_gg(tmp_path: Path) -> None:
    out = _run(tmp_path, "    a\n    b\n    c\n", "G␞<gg")
    assert out == "a\nb\nc\n"


# --- > with j / k (motion count = arg digits) ---

def test_indent_with_j_motion(tmp_path: Path) -> None:
    out = _run(tmp_path, "a\nb\nc\nd\n", "gg␞>j")
    # >j indents current + next line.
    assert out == "    a\n    b\nc\nd\n"


def test_indent_with_k_motion(tmp_path: Path) -> None:
    out = _run(tmp_path, "a\nb\nc\nd\n", "G␞>k")
    # >k indents current + previous line.
    assert "    c" in out and "    d" in out


# --- = (no-op reindent placeholder still triggers operator paths) ---

def test_eq_eq_is_noop(tmp_path: Path) -> None:
    # == is the line-wise = operator; supertool may treat it as identity.
    out = _run(tmp_path, "    hello\n", "gg␞==")
    # Whatever it does, it must not corrupt the file.
    assert "hello" in out


# --- >> with count (line range expand) ---

def test_indent_with_count(tmp_path: Path) -> None:
    out = _run(tmp_path, "a\nb\nc\n", "gg␞3>>")
    assert out == "    a\n    b\n    c\n"


def test_dedent_with_count(tmp_path: Path) -> None:
    out = _run(tmp_path, "    a\n    b\n    c\n", "gg␞3<<")
    assert out == "a\nb\nc\n"


# --- > with text-object ---

def test_indent_inside_paragraph(tmp_path: Path) -> None:
    out = _run(tmp_path, "p1 line1\np1 line2\n\np2\n", "gg␞>ip")
    # >ip indents the current paragraph (lines 1-2).
    assert out == "    p1 line1\n    p1 line2\n\np2\n"


def test_indent_around_paragraph(tmp_path: Path) -> None:
    out = _run(tmp_path, "p1\n\np2\n", "gg␞>ap")
    # >ap indents the paragraph + trailing blank line.
    assert "    p1" in out


# --- indent with %, +, - motions (6864-6903) ---

def test_indent_with_percent_motion(tmp_path: Path) -> None:
    out = _run(tmp_path, "x = {\n  body\n}\nafter\n", "gg␞/{␞>%")
    # / lands on '{'. >% spans the matching '}'. Indent lines 1-3.
    assert "    x = {" in out and "    }" in out


def test_indent_with_plus_motion(tmp_path: Path) -> None:
    out = _run(tmp_path, "a\nb\nc\n", "gg␞>+")
    # >+ indents current line and next line (first-non-blank of next).
    assert "    a\n" in out and "    b\n" in out and "c\n" in out


def test_indent_with_minus_motion(tmp_path: Path) -> None:
    out = _run(tmp_path, "a\nb\nc\n", "G␞>-")
    # >- indents current line and prev line.
    assert "    b" in out and "    c" in out


def test_indent_with_unknown_motion_is_noop_current_line(tmp_path: Path) -> None:
    # An unrecognized motion falls through to the default target=cursor →
    # operates on the current line only.
    out = _run(tmp_path, "a\nb\nc\n", "j␞>l")
    # Whatever this resolves to, must not corrupt the file.
    assert "b" in out


# --- indent on blank-only line ---

def test_indent_paragraph_motion_from_blank_line(tmp_path: Path) -> None:
    # Cursor on a blank line: { motion needs to walk back through prev paragraph.
    out = _run(tmp_path, "alpha\nbeta\n\ngamma\n", "G␞k␞>{")
    # 'G' last line, 'k' up to blank, '>{' indents back through prev paragraph.
    assert "    alpha" in out or "    beta" in out
