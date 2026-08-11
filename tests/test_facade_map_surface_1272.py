"""#1272 — a non-empty result from the entry-point shim reads as the module.

#1259 disclosed the facade only where the result was already **empty**, on the
argument that an absence is read as an absence in the world. `map:supertool.py`
is the harder half of the same defect: it returns the shim's two real symbols,
so the answer is small, correct, positive — and nothing about it prompts the
second call, because the reader has no reason to doubt a result that looks like
an answer.

The note therefore has to read as "there is more next door", never as a
correction: the symbols listed really are the shim's, and the map really is
complete for that file.
"""

from __future__ import annotations

from pathlib import Path

import supertool

from _changelog_findable import assert_change_is_findable


def _shim_pair(tmp_path: Path) -> Path:
    """A `supertool.py` / `_supertool.py` pair, the shape #931 created.

    The shim carries real definitions on purpose — a shim with none would map
    to `(no symbols)` and land back in #1259's empty case, which is the one
    thing this issue is not about.
    """
    core = tmp_path / "_supertool.py"
    core.write_text("def op_needle():\n    return 1\n")
    shim = tmp_path / "supertool.py"
    shim.write_text(
        "def _marker_lines():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def _refuse_unimportable_core():\n"
        "    return 2\n"
    )
    return shim


def test_map_of_the_shim_says_the_surface_is_next_door(tmp_path: Path) -> None:
    shim = _shim_pair(tmp_path)
    out = supertool.dispatch(f"map:{shim}")
    assert "entry point" in out, out
    assert "_supertool.py" in out, out


def test_the_note_discloses_and_does_not_retract(tmp_path: Path) -> None:
    """The result is genuinely correct, so the note may not read as a fix.

    Two halves, and both are load-bearing: the symbols stay (disclosure, never
    redirect — #1259), and the wording does not tell the reader the answer was
    empty or wrong. `Re-run against` is #1259's own empty-case sentence; using
    it here would call a correct result a mistake.
    """
    shim = _shim_pair(tmp_path)
    out = supertool.dispatch(f"map:{shim}")
    assert "_marker_lines" in out, out
    assert "_refuse_unimportable_core" in out, out
    assert "complete" in out, out
    assert "Re-run against" not in out, out
    assert "An empty result here" not in out, out


def test_map_of_a_lone_supertool_py_says_nothing(tmp_path: Path) -> None:
    """The pair on disk is what makes it a facade (#1259). A file merely named
    `supertool.py` in someone else's tree is an ordinary file."""
    lone = tmp_path / "supertool.py"
    lone.write_text("def thing():\n    return 1\n")
    out = supertool.dispatch(f"map:{lone}")
    assert "thing" in out, out
    assert "entry point" not in out, out


def test_map_of_the_directory_holding_the_pair_says_nothing(
        tmp_path: Path) -> None:
    """A directory map enumerates both files, so `_supertool.py` is already on
    screen and there is nothing left to disclose."""
    _shim_pair(tmp_path)
    out = supertool.dispatch(f"map:{tmp_path}")
    assert "op_needle" in out, out
    assert "entry point" not in out, out


def test_map_of_an_unrelated_file_says_nothing(tmp_path: Path) -> None:
    (tmp_path / "_supertool.py").write_text("def op_needle():\n    return 1\n")
    other = tmp_path / "other.py"
    other.write_text("def thing():\n    return 1\n")
    out = supertool.dispatch(f"map:{other}")
    assert "entry point" not in out, out


def test_the_surface_note_predicate(tmp_path: Path) -> None:
    """The predicate itself, on every platform and every tier — the assertions
    above go through whichever of tree-sitter/ctags/regex the host has."""
    shim = _shim_pair(tmp_path)
    note = supertool._shim_facade_surface_note(str(shim))
    assert "_supertool.py" in note
    assert "entry point" in note

    lone_dir = tmp_path / "lone"
    lone_dir.mkdir()
    lone = lone_dir / "supertool.py"
    lone.write_text("def thing():\n    return 1\n")
    assert supertool._shim_facade_surface_note(str(lone)) == ""

    other = tmp_path / "other.py"
    other.write_text("def thing():\n    return 1\n")
    assert supertool._shim_facade_surface_note(str(other)) == ""
    assert supertool._shim_facade_surface_note("") == ""
    assert supertool._shim_facade_surface_note(str(tmp_path / "gone.py")) == ""
    assert supertool._shim_facade_surface_note(str(tmp_path)) == ""


def test_a_changelog_fragment_exists() -> None:
    assert_change_is_findable(1272)
