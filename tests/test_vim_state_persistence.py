"""Tests for vim state persistence helpers, NO_PERSIST env var,
and ? literal-mode autocorrect."""
from __future__ import annotations

from pathlib import Path

import supertool


def test_vim_save_cursor_respects_no_persist(tmp_path: Path, monkeypatch) -> None:
    """_vim_save_cursor must early-return when SUPERTOOL_VIM_NO_PERSIST is set."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.txt"
    f.write_text("hello\n")
    supertool._vim_save_cursor(str(f), 3)
    # No file should be created in the cache.
    state_path = Path(supertool._vim_cursor_state_path(str(f)))
    assert not state_path.exists()


def test_vim_save_cursor_preserves_marks(tmp_path: Path, monkeypatch) -> None:
    """_vim_save_cursor preserves existing marks/last_edit by reading then writing."""
    monkeypatch.delenv("SUPERTOOL_VIM_NO_PERSIST", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    f = tmp_path / "x.txt"
    f.write_text("hello world\n")
    state_path = Path(supertool._vim_cursor_state_path(str(f)))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Seed with marks via the full state writer.
        supertool._vim_save_state(str(f), 0, {"a": 5}, None)
        # Now use the shim — should keep the mark.
        supertool._vim_save_cursor(str(f), 7)
        state = supertool._vim_load_state(str(f), 100)
        assert state["cursor"] == 7
        assert state["marks"].get("a") == 5
    finally:
        state_path.unlink(missing_ok=True)


def test_vim_save_state_respects_no_persist(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.txt"
    f.write_text("hello\n")
    supertool._vim_save_state(str(f), 3, {}, None)
    state_path = Path(supertool._vim_cursor_state_path(str(f)))
    assert not state_path.exists()


def test_search_literal_fallback_with_special_regex_chars(tmp_path: Path) -> None:
    """Search patterns that fail as regex should fall back to literal."""
    f = tmp_path / "x.txt"
    f.write_text("a(b)c(d)\n")
    out = supertool.op_vim(str(f), "gg␞/(b)␞iX")
    # /(b) is invalid as regex (unbalanced) — autocorrect tries literal.
    assert "X" in f.read_text()


def test_question_search_literal_fallback_inline(tmp_path: Path) -> None:
    """Backward ? search with regex-invalid pattern uses literal mode."""
    f = tmp_path / "x.txt"
    f.write_text("a(b)c(d)\n")
    out = supertool.op_vim(str(f), "G␞$␞?(b)␞iX")
    assert "X" in f.read_text()
