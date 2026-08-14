"""map's tier-2 fallback must be reachable when tier 1 cannot parse (#913).

`use_ctags = not use_ts and _has_ctags()` made ctags dead for the whole run
whenever tree-sitter imported, so the per-file `if not symbols_found` fallback
never fired - including for a file whose tree-sitter grammar failed to load,
which is precisely what ctags exists to cover. The docs promised a per-file
1 -> 2 -> 3 cascade; the code did tree-sitter XOR ctags, then regex.

The fix is not "always try ctags": measured on this repo's own tree, 14 of 100
mapped files produce no tree-sitter symbols and every one of them is a file
with genuinely nothing to find (a changelog fragment, an empty __init__.py).
Spawning ctags for those costs ~41ms each on macOS and buys nothing, on the
same per-file-subprocess shape that has reddened Windows legs four times.
So the fallback is gated on the tree-sitter tier being *blind* to the file
rather than merely empty-handed.
"""

from pathlib import Path
from unittest.mock import patch

import supertool


def _ts_on() -> None:
    supertool._TS_CHECKED = True
    supertool._TS_AVAILABLE = True
    supertool._TS_PACKAGE = "pack"


def test_ctags_runs_when_tree_sitter_grammar_failed(tmp_path: Path) -> None:
    """tree-sitter imported but its .swift grammar never loaded -> tier 2."""
    f = tmp_path / "Thing.swift"
    f.write_text("class Thing {}\n")
    _ts_on()

    fake_ctags = [("class", "Thing", 1, "")]
    with patch.dict(supertool._TS_GRAMMAR_FAILED, {"swift": "no such grammar"}), \
         patch.object(supertool, "_has_tree_sitter", return_value=True), \
         patch.object(supertool, "_has_ctags", return_value="/usr/bin/ctags"), \
         patch.object(supertool, "_ctags_extract", return_value=fake_ctags) as ct:
        out = supertool.op_map(str(f))

    assert ct.called, "ctags tier was never consulted"
    assert "tier: ctags" in out
    assert "class Thing" in out


def test_ctags_reason_note_reflects_a_run_not_availability(tmp_path: Path) -> None:
    """The '; ctags found nothing' note means ctags ran, not that it exists."""
    f = tmp_path / "Thing.swift"
    f.write_text("class Thing {}\n")
    _ts_on()

    with patch.dict(supertool._TS_GRAMMAR_FAILED, {"swift": "no such grammar"}), \
         patch.object(supertool, "_has_tree_sitter", return_value=True), \
         patch.object(supertool, "_has_ctags", return_value="/usr/bin/ctags"), \
         patch.object(supertool, "_ctags_extract", return_value=[]):
        out = supertool.op_map(str(f))

    assert "no symbol parser for .swift" in out
    assert "ctags found nothing" in out


def test_ctags_not_spawned_when_tree_sitter_parsed_and_found_nothing(
    tmp_path: Path,
) -> None:
    """A parsed file with no definitions must not cost a subprocess.

    Regression pin for the rejected unconditional form of the #913 fix, which
    would spawn ctags for every symbol-less file. Green before and after; it
    exists to stay green.
    """
    f = tmp_path / "empty.py"
    f.write_text("x = 1\n")
    _ts_on()

    with patch.object(supertool, "_has_tree_sitter", return_value=True), \
         patch.object(supertool, "_ts_extract", return_value=[]), \
         patch.object(supertool, "_has_ctags", return_value="/usr/bin/ctags"), \
         patch.object(supertool, "_ctags_extract", return_value=[]) as ct:
        supertool.op_map(str(f))

    assert not ct.called, "ctags spawned for a file tree-sitter could parse"


def test_ctags_still_precedes_regex_when_tree_sitter_absent(tmp_path: Path) -> None:
    """No tree-sitter: every file still goes 2 -> 3, as before (#913)."""
    f = tmp_path / "example.py"
    f.write_text("class Foo:\n    pass\n")

    fake_ctags = [("class", "Foo", 1, "")]
    with patch.object(supertool, "_has_tree_sitter", return_value=False), \
         patch.object(supertool, "_has_ctags", return_value="/usr/bin/ctags"), \
         patch.object(supertool, "_ctags_extract", return_value=fake_ctags) as ct:
        out = supertool.op_map(str(f))

    assert ct.called
    assert "tier: ctags" in out


def test_ctags_runs_for_a_named_file_no_tier_1_extension(tmp_path: Path) -> None:
    """The docs' own case: `map:notes.rst` with ctags installed.

    A directory walk only visits extensions some tier claims, so this is
    reached by naming the file. Before #913 the ctags tier was dead here
    whenever tree-sitter imported - which is every developer machine with the
    language pack installed, i.e. exactly the reader the ctags install
    instructions are written for.
    """
    f = tmp_path / "notes.rst"
    f.write_text("Title\n=====\n")
    _ts_on()

    fake_ctags = [("section", "Title", 1, "")]
    with patch.object(supertool, "_has_tree_sitter", return_value=True), \
         patch.object(supertool, "_has_ctags", return_value="/usr/bin/ctags"), \
         patch.object(supertool, "_ctags_extract", return_value=fake_ctags) as ct:
        out = supertool.op_map(str(f))

    assert ct.called, "ctags never consulted for an extension no tier 1 claims"
    assert "tier: ctags" in out
    assert "section Title" in out
