"""A read op's honest zero that answers a question nobody asked (#1259, #1261).

Both are the same shape: the op resolved a path, did exactly what it was told,
and returned a result whose *reading* is wrong.

#1259 — `supertool.py` is the entry-point shim; the code lives in
`_supertool.py` beside it. A grep of the shim that finds nothing is
byte-identical to a grep of the core that finds nothing, and after the split
every historical instinct to grep `supertool.py` lands on the shim.

#1261 — two whitespace-separated paths in the one PATH slot fail as a single
missing filename, and the refusal blames the `:` split, which is not what
happened and whose prescribed repair (a payload) reproduces the failure.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import supertool


def _tree_sitter_installed() -> bool:
    """Is a tree-sitter language pack importable HERE?

    `enable_tree_sitter` only clears supertool's cached detection flags; it
    cannot install the package, and CI legs have no pack — so a test gated on
    this one is skipped on every leg and proves nothing there. That is why the
    `between` wiring is pinned by a direct test of `_shim_facade_note` as
    well, which runs everywhere.
    """
    for name in ("tree_sitter_language_pack", "tree_sitter_languages"):
        try:
            if importlib.util.find_spec(name) is not None:
                return True
        except (ImportError, ValueError):
            continue
    return False


_HAS_TREE_SITTER = _tree_sitter_installed()


def _shim_pair(tmp_path: Path) -> Path:
    """A `supertool.py` / `_supertool.py` pair, the shape #931 created."""
    core = tmp_path / "_supertool.py"
    core.write_text("def op_needle():\n    return 1\n")
    shim = tmp_path / "supertool.py"
    shim.write_text("import _supertool\n")
    return shim


# --- #1259: a zero from the facade ---------------------------------------


def test_grep_zero_on_the_shim_names_the_core(tmp_path: Path) -> None:
    shim = _shim_pair(tmp_path)
    out = supertool.dispatch(f"grep:def op_needle:{shim}:10:0")
    assert "0 results" in out, out
    assert "_supertool.py" in out, out
    assert "entry point" in out, out


def test_grep_with_a_hit_on_the_shim_says_nothing(tmp_path: Path) -> None:
    """The note is about an EMPTY result. A hit is evidence about the shim
    and needs no disclaimer."""
    shim = _shim_pair(tmp_path)
    out = supertool.dispatch(f"grep:import:{shim}:10:0")
    assert "1 results" in out, out
    assert "entry point" not in out, out


def test_lone_supertool_py_with_no_core_beside_it_says_nothing(
        tmp_path: Path) -> None:
    """The pair is what makes it a facade. A file merely named `supertool.py`
    is an ordinary file, and a note there would be a guess."""
    lone = tmp_path / "supertool.py"
    lone.write_text("print(1)\n")
    out = supertool.dispatch(f"grep:def op_needle:{lone}:10:0")
    assert "0 results" in out, out
    assert "entry point" not in out, out


def test_around_no_match_on_the_shim_names_the_core(tmp_path: Path) -> None:
    shim = _shim_pair(tmp_path)
    out = supertool.dispatch(f"around:def op_needle:{shim}:2")
    assert "no match" in out, out
    assert "_supertool.py" in out, out
    assert "entry point" in out, out


@pytest.mark.skipif(not _HAS_TREE_SITTER,
                    reason="between symbol mode needs a tree-sitter pack")
def test_between_symbol_not_found_on_the_shim_names_the_core(
        tmp_path: Path, enable_tree_sitter) -> None:
    shim = _shim_pair(tmp_path)
    out = supertool.dispatch(f"between:op_needle:{shim}")
    assert "not found" in out, out
    assert "_supertool.py" in out, out
    assert "entry point" in out, out


def test_the_note_predicate_needs_the_pair_on_disk(tmp_path: Path) -> None:
    """The predicate itself, on every platform — the assertion above is
    skipped wherever no tree-sitter pack is installed, which is every CI leg,
    and a skipped test is not a covered branch."""
    shim = _shim_pair(tmp_path)
    assert "_supertool.py" in supertool._shim_facade_note(str(shim))

    lone_dir = tmp_path / "lone"
    lone_dir.mkdir()
    lone = lone_dir / "supertool.py"
    lone.write_text("print(1)\n")
    assert supertool._shim_facade_note(str(lone)) == ""

    other = tmp_path / "other.py"
    other.write_text("print(1)\n")
    assert supertool._shim_facade_note(str(other)) == ""
    assert supertool._shim_facade_note("") == ""
    assert supertool._shim_facade_note(str(tmp_path / "gone.py")) == ""


def test_a_zero_over_an_ordinary_file_is_untouched(tmp_path: Path) -> None:
    f = tmp_path / "thing.py"
    f.write_text("pass\n")
    out = supertool.dispatch(f"grep:def op_needle:{f}:10:0")
    assert "0 results" in out, out
    assert "entry point" not in out, out


# --- #1261: two paths in the one PATH slot -------------------------------


@pytest.fixture
def two_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`a.py` and `b.py` in the cwd, named RELATIVELY in every call below.

    Not a style choice. `_split_arg` reassembles a Windows drive letter only
    when the piece before the ':' ends in a single letter after a ','-split,
    so a drive letter sitting after a SPACE never rejoins: an absolute
    two-path value tokenizes on Windows into something neither the caller nor
    the assertion typed. Written with absolute paths these tests would assert
    one thing on POSIX and a different thing on Windows, which is the #1004
    shape. Relative paths carry no drive letter, so the call means the same on
    all three platforms. (The product has the same gap for absolute paths on
    Windows; widening the tokenizer is a separate decision, not this one.)
    """
    (tmp_path / "a.py").write_text("needle\n")
    (tmp_path / "b.py").write_text("needle\n")
    monkeypatch.chdir(tmp_path)


def test_grep_two_paths_is_named_not_blamed_on_the_colon(two_files) -> None:
    out = supertool.dispatch("grep:needle:a.py b.py:10:0")
    assert "TWO paths" in out, out
    assert "ONE path" in out, out
    assert "split on" not in out, out
    assert "@-" not in out, out


def test_grep_two_paths_offers_the_batched_repair(two_files) -> None:
    """Batching is the premise of the tool and it is the answer here: two
    greps in one call is the single round-trip the caller reached for."""
    out = supertool.dispatch("grep:needle:a.py b.py:10:0")
    assert "grep:needle:a.py" in out, out
    assert "grep:needle:b.py" in out, out


def test_two_paths_where_one_is_missing_keeps_the_colon_hint(
        two_files) -> None:
    """Three states. The disclosure is only warranted when the tool can
    positively say every part resolves; otherwise this is a genuinely
    missing path and the existing message is correct."""
    out = supertool.dispatch("grep:needle:a.py nope.py:10:0")
    assert "TWO paths" not in out, out
    assert "split on" in out, out


def test_read_two_paths_is_named_too(two_files) -> None:
    """`read` never reaches `_colon_split_hint`, so it used to answer with
    `wrong CWD?` — the one cause that provably did not apply."""
    out = supertool.dispatch("read:a.py b.py")
    assert "TWO paths" in out, out
    assert "wrong CWD?" not in out, out


def test_map_two_paths_is_named_too(two_files) -> None:
    out = supertool.dispatch("map:a.py b.py")
    assert "TWO paths" in out, out
    assert "wrong CWD?" not in out, out


def test_a_filename_containing_a_space_still_reads(two_files) -> None:
    """The reason `grep` does not simply accept a whitespace-separated list:
    this file would become unrepresentable. It must keep working."""
    Path("two words.py").write_text("needle\n")
    out = supertool.dispatch("grep:needle:two words.py:10:0")
    assert "needle" in out, out
    assert "TWO paths" not in out, out


def test_a_missing_spaced_filename_is_still_an_ordinary_miss(
        two_files) -> None:
    out = supertool.dispatch("read:no such.py")
    assert "TWO paths" not in out, out


def test_three_paths_are_counted_as_three(two_files) -> None:
    Path("c.py").write_text("needle\n")
    out = supertool.dispatch("grep:needle:a.py b.py c.py:10:0")
    assert "THREE paths" in out, out
