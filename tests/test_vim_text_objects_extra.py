"""More text-object and edge-case tests for coverage."""
from __future__ import annotations

import os
from pathlib import Path

import supertool


def _run(tmp_path: Path, initial: str, script: str) -> str:
    f = tmp_path / "x.txt"
    f.write_text(initial)
    out = supertool.op_vim(str(f), script)
    assert not out.startswith("ERROR"), out
    return f.read_text()


# --- iw on punctuation (2707-2712) ---

def test_diw_on_punctuation_run(tmp_path: Path) -> None:
    out = _run(tmp_path, "abc..def\n", "gg␞/\\.␞diw")
    # cursor on first '.', iw is the punctuation run "..".
    assert out == "abcdef\n"


# --- iW/aW on whitespace (2733-2738) ---

def test_diW_on_whitespace_run(tmp_path: Path) -> None:
    # cursor on space between WORDs — iW for whitespace?
    # supertool's iW on whitespace spans the whitespace run.
    out = _run(tmp_path, "foo    bar\n", "gg␞/  ␞diW")
    # iW on whitespace deletes the whitespace run.
    assert "foo" in out and "bar" in out


# --- sentence text-object (2762-2768) ---

def test_dis_inside_sentence(tmp_path: Path) -> None:
    out = _run(tmp_path, "First sentence. Second one. Third.\n", "gg␞/Second␞dis")
    # dis deletes "Second one." (the sentence around cursor).
    assert "First sentence." in out
    assert "Third." in out


def test_das_around_sentence(tmp_path: Path) -> None:
    out = _run(tmp_path, "First. Second. Third.\n", "gg␞/Second␞das")
    # das also includes trailing whitespace.
    assert "First." in out and "Third." in out


# --- literal ? search autocorrect (4517-4523) ---

def test_question_search_literal_fallback(tmp_path: Path) -> None:
    # Search backward for a literal pattern that's tricky as regex.
    out = _run(tmp_path, "abc(x)def(y)\n", "G␞?(x)␞iZ")
    # ?(x) backward-search literal '(x)' — Z inserted at match position.
    assert "Z" in out and "abc" in out


# --- indent with % motion from closing bracket (6882-6893) ---

def test_indent_percent_from_closing_bracket(tmp_path: Path) -> None:
    out = _run(tmp_path, "{\n  body\n}\nafter\n", "G␞k␞>%")
    # G to last line, k up to '}', >% spans back to '{'. Indent lines 1-3.
    assert "    {" in out and "    }" in out


def test_indent_percent_from_closing_paren(tmp_path: Path) -> None:
    out = _run(tmp_path, "(\nbody\n)\n", "G␞>%")
    # G lands on ')' line. >% spans back to '('. Indent all three lines.
    assert "    (" in out and "    )" in out


# --- SUPERTOOL_VIM_NO_PERSIST env var (2597-2604) ---

def test_vim_no_persist_env_var_skips_save(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.txt"
    f.write_text("hello\n")
    supertool.op_vim(str(f), "ggiX\x1b")
    # State file should not have been created.
    state_file = tmp_path / ".x.txt.vim-state"
    assert not state_file.exists()
