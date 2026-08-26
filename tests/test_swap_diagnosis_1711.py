"""#1711 -- `around`'s "Read as pattern=X + path=Y" diagnosis exists but is
unreachable from the 3-arg form, which is the arity that most often means the
args were swapped: `around:PATTERN:PATH` with the two swapped gave
`ERROR: file not found: <pattern>` plus `wrong CWD?` -- a remedy that does not
fix the problem, since the cwd was fine and the thing it calls a file is a
pattern.

`_colon_split_hint` (the existing "Read as ... split on ':'" diagnosis) only
fires when the leading argument has a ':' in it or the missing path token
does not look like a plain path -- neither holds for a plain swap where both
values are ordinary identifier-shaped tokens, which is exactly the shape a
swap most often takes. This closes that gap generically: whenever the
"path" a read op computed does not exist AND the argument sitting in the
OTHER slot does exist as a real file/dir, that is strong evidence of a swap
and the op says so instead of blaming the cwd.

Every "must fire" case here is paired with a "must not fire" one in the same
op, per the same-fixture rule for a silence assertion: a positive control
that the ordinary not-a-typo error still renders when nothing looks swapped.
"""

from __future__ import annotations

from pathlib import Path

import supertool


def _write(tmp_path: Path, name: str, body: str = "x\n") -> Path:
    f = tmp_path / name
    f.write_text(body, encoding="utf-8")
    return f


# --- around (the issue's own reproduction) ------------------------------

def test_around_swap_names_the_swap_not_wrong_cwd(tmp_path: Path) -> None:
    real = _write(tmp_path, "real.py", "needle here\n")
    # Caller meant around:needle:real.py -- typed swapped.
    out = supertool.dispatch(f"around:{real}:needle")
    assert "wrong CWD" not in out, out
    assert "Did you mean" in out, out
    assert str(real) in out, out
    assert "needle" in out, out


def test_around_ordinary_missing_path_still_says_wrong_cwd(tmp_path: Path) -> None:
    """Must-not-fire half: neither slot is a real file, so this is an
    ordinary typo/cwd-drift case and the swap message must not fire."""
    out = supertool.dispatch("around:needle:definitely/does/not/exist.py")
    assert "wrong CWD" in out, out
    assert "Did you mean" not in out, out


# --- grep -----------------------------------------------------------------

def test_grep_swap_names_the_swap(tmp_path: Path) -> None:
    real = _write(tmp_path, "real2.py", "needle here\n")
    out = supertool.dispatch(f"grep:{real}:needle")
    assert "wrong CWD" not in out, out
    assert "Did you mean" in out, out
    assert str(real) in out, out


def test_grep_ordinary_missing_path_still_says_wrong_cwd(tmp_path: Path) -> None:
    out = supertool.dispatch("grep:needle:definitely/does/not/exist.py")
    assert "wrong CWD" in out, out
    assert "Did you mean" not in out, out


# --- between (symbol mode) -------------------------------------------------

def test_between_symbol_swap_names_the_swap(tmp_path: Path) -> None:
    real = _write(tmp_path, "real3.py", "def foo():\n    pass\n")
    # Caller meant between:foo:real3.py -- typed swapped.
    out = supertool.dispatch(f"between:{real}:foo")
    assert "Did you mean" in out, out
    assert str(real) in out, out


def test_between_symbol_ordinary_missing_path_is_unchanged(tmp_path: Path) -> None:
    out = supertool.dispatch("between:foo:definitely/does/not/exist.py")
    assert "not found" in out, out
    assert "Did you mean" not in out, out
