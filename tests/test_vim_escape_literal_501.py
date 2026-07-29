"""`\\\\e` — the escape hatch for a literal backslash-e in a vim script (#501).

`\\e` is the ESC marker, and `_op_vim_impl` substituted it over the **whole** raw
script — including greedy captures like insert TEXT and `:!` command text — so a
literal `\\e` in content could not be expressed at all.

That is not exotic content. A Windows path segment beginning with `e`
(`\\emit.py`, `\\explorer.exe`, `\\env`) contains it, and the collision truncated
whatever capture it landed inside, silently and mid-action. It surfaced as four
red Windows legs on #506 whose fixture happened to write a file named `emit.py`;
the guard under test never ran, because parsing broke before it.

Both directions are pinned here: the escape produces a literal, and the ESC
marker itself still works.
"""

from pathlib import Path

import supertool


def test_escaped_backslash_e_survives_as_a_literal(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hello\n", encoding="utf-8")
    supertool.op_vim(str(f), r"Goliteral \\e path")
    assert f.read_text(encoding="utf-8") == "hello\nliteral \\e path\n"


def test_a_windows_path_segment_no_longer_truncates_the_action(tmp_path: Path) -> None:
    """The #506 regression: `\\emit.py` ate the marker and cut the insert short."""
    f = tmp_path / "x.txt"
    f.write_text("hello\n", encoding="utf-8")
    supertool.op_vim(str(f), r"GoC:\\emit.py --flag")
    written = f.read_text(encoding="utf-8")
    assert written == "hello\nC:\\emit.py --flag\n"
    assert "--flag" in written, "the tail of the capture must survive the escape"


def test_the_esc_marker_itself_still_works(tmp_path: Path) -> None:
    """Negative control: a single `\\e` must still terminate insert mode.

    Without this, 'never substitute anything' would pass the two tests above.
    """
    f = tmp_path / "x.txt"
    f.write_text("hello\n", encoding="utf-8")
    supertool.op_vim(str(f), "Goinserted\\eA!")
    assert f.read_text(encoding="utf-8") == "hello\ninserted!\n"
