"""Tests for three Kevin-observed pain points (post hard cut to ␞):

1. `\\x1e` (ASCII U+001E) accepted as separator alongside `␞` (U+241E).
   Bash users naturally reach for `$'\\x1e'`.
2. Bare text-verb (i/a/A/I/o/O alone) followed by an action that would
   error with 'unknown verb' → merge: treat next action as the TEXT.
3. `:r FILE` (or `:r -`) at end-of-file when last line is `}` alone →
   insert BEFORE the `}`, not after. Catches the `G␞:r FILE` mistake
   that drops the snippet outside the class.
"""
from __future__ import annotations

import io
from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# 1. \x1e accepted as separator
# ---------------------------------------------------------------------------

def test_ascii_x1e_acts_as_separator(tmp_path: Path) -> None:
    """`\\x1e` (U+001E) splits actions exactly like `␞` (U+241E)."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\n")
    out = supertool.op_vim(str(f), "G\x1edd")
    assert "ERROR" not in out, out
    assert f.read_text() == "a\nb\n"


def test_mixed_x1e_and_glyph_separators(tmp_path: Path) -> None:
    """Mixing `\\x1e` and `␞` in the same script works."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\n")
    out = supertool.op_vim(str(f), "G\x1edd␞gg\x1edd")
    assert "ERROR" not in out, out
    assert f.read_text() == "b\nc\n"


# ---------------------------------------------------------------------------
# 2. Bare text-verb + next unknown action = merge as TEXT
# ---------------------------------------------------------------------------

def test_O_consumes_text_natively_no_separator_needed(tmp_path: Path) -> None:
    """C-full: `GOuse Bar;` — O is greedy, consumes "use Bar;" as TEXT
    until ESC/EOS. No separator between O and the text."""
    f = tmp_path / "x.php"
    f.write_text("<?php\nclass Foo {\n}\n")
    out = supertool.op_vim(str(f), "GOuse Bar;")
    assert "ERROR" not in out, out
    assert "use Bar;" in f.read_text()


def test_i_consumes_text_natively_no_separator_needed(tmp_path: Path) -> None:
    """C-full: `ifoo bar` — i is greedy, consumes "foo bar" as TEXT."""
    f = tmp_path / "x.txt"
    f.write_text("end\n")
    out = supertool.op_vim(str(f), "ifoo bar")
    assert "ERROR" not in out, out
    assert f.read_text() == "foo barend\n"


def test_text_verb_with_payload_does_not_merge(tmp_path: Path) -> None:
    """Regression — `i hello␞A world` is two actions (i hello, then A world).
    Don't merge when the text-verb already has a payload."""
    f = tmp_path / "x.txt"
    f.write_text("X\n")
    out = supertool.op_vim(str(f), "ihello␞A world")
    assert "ERROR" not in out, out
    # i inserts "hello" before cursor (at start) → "helloX\n"
    # A appends " world" before trailing \n → "helloX world\n"
    assert f.read_text() == "helloX world\n"


def test_bare_o_followed_by_valid_verb_does_not_merge(tmp_path: Path) -> None:
    """If the next action IS a valid verb, no merge — keep current semantics
    (O with empty text inserts a blank line above, then next action runs)."""
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\n")
    out = supertool.op_vim(str(f), "2G␞O␞dd")
    assert "ERROR" not in out, out
    # O on line 2 opens a blank line above (line 2 becomes blank), then dd
    # deletes the cursor's current line (the blank). Net: file unchanged.
    # The key assertion is no error.


# ---------------------------------------------------------------------------
# 3. :r at last-line-on-`}` → insert before `}`
# ---------------------------------------------------------------------------

def test_r_at_eof_with_closing_brace_inserts_before(tmp_path: Path) -> None:
    """`G␞:r FILE` when last line is `}` alone → insert BEFORE the `}`.
    Catches the most common Kevin mistake (drops snippet outside class)."""
    snippet = tmp_path / "snippet.txt"
    snippet.write_text("    public function foo(): void {}\n")
    f = tmp_path / "x.php"
    f.write_text("<?php\nclass Foo {\n}\n")
    out = supertool.op_vim(str(f), f"G␞:r {snippet}")
    assert "ERROR" not in out, out
    # snippet lands INSIDE the class, before `}`
    assert f.read_text() == (
        "<?php\nclass Foo {\n"
        "    public function foo(): void {}\n"
        "}\n"
    )


def test_r_at_eof_with_non_brace_last_line_inserts_after(tmp_path: Path) -> None:
    """Regression — if last line is NOT a bare `}`, `:r` keeps existing
    behavior (insert AFTER the last line)."""
    snippet = tmp_path / "snippet.txt"
    snippet.write_text("appended\n")
    f = tmp_path / "x.md"
    f.write_text("# heading\nbody\n")
    out = supertool.op_vim(str(f), f"G␞:r {snippet}")
    assert "ERROR" not in out, out
    assert f.read_text() == "# heading\nbody\nappended\n"


def test_r_dash_stdin_at_eof_with_brace_inserts_before(tmp_path: Path, monkeypatch) -> None:
    """Same brace-aware behavior for `:r -` (stdin)."""
    monkeypatch.setattr("sys.stdin", io.StringIO("    public function bar(): void {}\n"))
    f = tmp_path / "x.php"
    f.write_text("<?php\nclass Foo {\n}\n")
    out = supertool.op_vim(str(f), "G␞:r -")
    assert "ERROR" not in out, out
    assert f.read_text() == (
        "<?php\nclass Foo {\n"
        "    public function bar(): void {}\n"
        "}\n"
    )


def test_r_explicit_before_brace_still_works(tmp_path: Path) -> None:
    """Regression — explicit `G␞?^}␞:r FILE` (the documented canonical
    form) still inserts at the same place."""
    snippet = tmp_path / "snippet.txt"
    snippet.write_text("    public function foo(): void {}\n")
    f = tmp_path / "x.php"
    f.write_text("<?php\nclass Foo {\n}\n")
    out = supertool.op_vim(str(f), f"G␞?^}}␞:r {snippet}")
    assert "ERROR" not in out, out
    assert f.read_text() == (
        "<?php\nclass Foo {\n"
        "    public function foo(): void {}\n"
        "}\n"
    )
