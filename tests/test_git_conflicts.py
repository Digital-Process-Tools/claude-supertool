"""Unit tests for presets/git/conflicts.py — block extraction across markers."""
from __future__ import annotations

import importlib.util
from pathlib import Path


PRESET = Path(__file__).parent.parent / "presets" / "git" / "conflicts.py"
_spec = importlib.util.spec_from_file_location("git_conflicts", PRESET)
assert _spec is not None and _spec.loader is not None
conflicts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(conflicts)


def test_extracts_all_blocks(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text(
        "before\n"
        "<<<<<<< HEAD\n"
        "ours_a\n"
        "=======\n"
        "theirs_a\n"
        ">>>>>>> branch\n"
        "middle\n"
        "<<<<<<< HEAD\n"
        "ours_b\n"
        "=======\n"
        "theirs_b\n"
        ">>>>>>> branch\n"
    )
    out = conflicts._all_conflict_blocks(str(f), max_lines_per_block=20)
    assert "block 1" in out and "block 2" in out
    assert "ours_a" in out and "theirs_a" in out
    assert "ours_b" in out and "theirs_b" in out


def test_truncates_long_block(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    body = "\n".join(f"line{i}" for i in range(50))
    f.write_text(f"<<<<<<< HEAD\n{body}\n=======\nx\n>>>>>>> branch\n")
    out = conflicts._all_conflict_blocks(str(f), max_lines_per_block=5)
    assert "truncated at 5" in out


def test_no_markers_message(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("clean file\n")
    out = conflicts._all_conflict_blocks(str(f), max_lines_per_block=10)
    assert "no <<<<<<< marker" in out
