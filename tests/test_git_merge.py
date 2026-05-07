"""Unit tests for presets/git/merge.py — conflict block helpers."""
from __future__ import annotations

import importlib.util
from pathlib import Path


PRESET = Path(__file__).parent.parent / "presets" / "git" / "merge.py"
_spec = importlib.util.spec_from_file_location("git_merge", PRESET)
assert _spec is not None and _spec.loader is not None
merge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merge)


def test_first_block_extracts_markers(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text(
        "before\n"
        "<<<<<<< HEAD\n"
        "ours\n"
        "=======\n"
        "theirs\n"
        ">>>>>>> branch\n"
        "after\n"
    )
    out = merge._first_conflict_block(str(f), max_lines=20)
    assert "<<<<<<< HEAD" in out
    assert "ours" in out
    assert "theirs" in out
    assert ">>>>>>> branch" in out
    assert "L2:" in out  # marker line numbers preserved


def test_first_block_truncates_long_block(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    body = "\n".join(f"line{i}" for i in range(50))
    f.write_text(f"<<<<<<< HEAD\n{body}\n=======\nx\n>>>>>>> branch\n")
    out = merge._first_conflict_block(str(f), max_lines=5)
    assert "truncated at 5" in out


def test_count_blocks(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text(
        "<<<<<<< HEAD\na\n=======\nb\n>>>>>>> br\n"
        "<<<<<<< HEAD\nc\n=======\nd\n>>>>>>> br\n"
    )
    assert merge._count_blocks(str(f)) == 2


def test_no_markers_message(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("plain\n")
    out = merge._first_conflict_block(str(f), max_lines=10)
    assert "no <<<<<<< marker" in out
