"""Three follow-up fixes from Kevin's third post-`␞`-cut run.

A. Refine bare-text-verb merge — skip if next action is `:`/`/`/`?`-prefixed.
   PR #58's merge wrongly absorbed `O␞:r FILE` (treated `:r FILE` as TEXT
   instead of an ex command). Same for `O␞:s/...`, `O␞/foo`, `O␞?bar`.

B. `<digits>gg` → `<digits>G` autocorrect. Kevin wrote `224gg` thinking it
   jumped to line 224; real vim semantics: `gg` ignores count, use `224G`.

C. `/PAT` miss-forward retry from BOF. Cursor persists across `vi:::` calls
   (via _vim_save_cursor). Kevin assumes BOF on each call; when search
   misses forward from a persisted mid-file cursor, silently retry from
   BOF. Note the retry in the receipt.
"""
from __future__ import annotations

import os
from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# A. Refine bare-text-verb merge — skip ex/search prefixed next action
# ---------------------------------------------------------------------------

def test_O_followed_by_r_ex_command_does_not_merge(tmp_path: Path, monkeypatch) -> None:
    """`G␞O␞:r FILE` — `:r` must be the ex command, NOT TEXT for `O`."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    snippet = tmp_path / "snippet.txt"
    snippet.write_text("inserted\n")
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\n")
    out = supertool.op_vim(str(f), f"G␞O␞:r {snippet}")
    assert "ERROR" not in out, out
    # The literal text ":r {snippet}" must NOT appear in the file.
    assert ":r " not in f.read_text(), f.read_text()
    assert "inserted" in f.read_text()


def test_O_followed_by_s_substitute_does_not_merge(tmp_path: Path, monkeypatch) -> None:
    """`O␞:s/foo/X/g` — `:s` is the substitute verb, not TEXT."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.txt"
    f.write_text("foo\n")
    out = supertool.op_vim(str(f), "G␞O␞:s/foo/X/g")
    assert "ERROR" not in out, out
    # Substitute should have run; `O` with empty TEXT opened a blank line above
    assert "X" in f.read_text()
    assert ":s/" not in f.read_text()


def test_O_followed_by_search_does_not_merge(tmp_path: Path, monkeypatch) -> None:
    """`o␞/bar` — `/bar` is a search action, not TEXT."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.txt"
    f.write_text("foo\nbar\nbaz\n")
    out = supertool.op_vim(str(f), "gg␞o␞/bar")
    assert "ERROR" not in out, out
    # `/bar` should NOT have been inserted as text
    assert "/bar" not in f.read_text(), f.read_text()


def test_O_consumes_text_natively_under_stateful_parser(tmp_path: Path, monkeypatch) -> None:
    """Under C-full stateful parser, `GOuse Bar;` works without any
    separator between O and the text — O is greedy, consumes "use Bar;"
    until ESC/EOS."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.php"
    f.write_text("<?php\nclass Foo {\n}\n")
    out = supertool.op_vim(str(f), "GOuse Bar;")
    assert "ERROR" not in out, out
    assert "use Bar;" in f.read_text()


# ---------------------------------------------------------------------------
# B. <digits>gg → <digits>G
# ---------------------------------------------------------------------------

def test_count_gg_autocorrects_to_G(tmp_path: Path, monkeypatch) -> None:
    """`5gg` should jump to line 5 (real vim uses `5G` for that)."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\nd\ne\nf\n")
    out = supertool.op_vim(str(f), "5gg␞dd")
    assert "ERROR" not in out, out
    # Cursor jumped to line 5 ("e"), dd removed it
    assert f.read_text() == "a\nb\nc\nd\nf\n"


def test_bare_gg_still_goes_to_bof(tmp_path: Path, monkeypatch) -> None:
    """Regression — `gg` alone (no count) still means go-to-BOF."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.txt"
    f.write_text("a\nb\nc\n")
    out = supertool.op_vim(str(f), "G␞gg␞iX")
    assert "ERROR" not in out, out
    # G → last line, gg → BOF, iX → insert X before 'a'
    assert f.read_text() == "Xa\nb\nc\n"


# ---------------------------------------------------------------------------
# C. /PAT miss-forward retry from BOF
# ---------------------------------------------------------------------------

def test_search_miss_retries_from_bof(tmp_path: Path) -> None:
    """When `/PAT` misses forward AND cursor was persisted mid-file, retry
    from BOF. Catches the Kevin pattern of forgetting cursor persists."""
    persist_dir = tmp_path / "persist"
    persist_dir.mkdir()
    # Use a unique env so we control persistence behavior
    os.environ.pop("SUPERTOOL_VIM_NO_PERSIST", None)
    os.environ["SUPERTOOL_VIM_PERSIST_DIR"] = str(persist_dir)

    f = tmp_path / "x.txt"
    f.write_text("foo\nbar\nbaz\nquux\n")
    # First call: jump to line 3 ('baz'), persists cursor there
    out1 = supertool.op_vim(str(f), "3G")
    assert "ERROR" not in out1, out1
    # Second call: search for 'foo' (line 1). Forward miss from cursor (line 3),
    # but autocorrect retries from BOF and finds it.
    out2 = supertool.op_vim(str(f), "/foo␞iX")
    assert "ERROR" not in out2, out2
    assert f.read_text() == "Xfoo\nbar\nbaz\nquux\n"

    os.environ.pop("SUPERTOOL_VIM_PERSIST_DIR", None)


def test_search_still_misses_when_truly_absent(tmp_path: Path, monkeypatch) -> None:
    """Regression — if pattern doesn't exist anywhere, still error."""
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.txt"
    f.write_text("foo\nbar\n")
    out = supertool.op_vim(str(f), "/notthere")
    assert "ERROR" in out
    assert "not found" in out
