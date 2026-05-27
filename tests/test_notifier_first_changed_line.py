"""Issue #236 — mutating-op notifier must compute the first changed line
from pre_content vs the post-edit file, so observers (cursor-witness) can
scroll the diff view to the edit instead of parking at line 1."""
from __future__ import annotations

from pathlib import Path

import supertool


def test_first_changed_line_middle_edit(tmp_path: Path) -> None:
    pre = b"a\nb\nc\nd\ne\n"
    f = tmp_path / "f.txt"
    f.write_bytes(b"a\nb\nCHANGED\nd\ne\n")
    assert supertool._first_changed_line(pre, str(f)) == 3


def test_first_changed_line_first_line_edit(tmp_path: Path) -> None:
    pre = b"a\nb\nc\n"
    f = tmp_path / "f.txt"
    f.write_bytes(b"X\nb\nc\n")
    assert supertool._first_changed_line(pre, str(f)) == 1


def test_first_changed_line_tail_insert(tmp_path: Path) -> None:
    pre = b"a\nb\nc\n"
    f = tmp_path / "f.txt"
    f.write_bytes(b"a\nb\nc\nd\ne\n")
    # Shared prefix is 3 lines; divergence at line 4.
    assert supertool._first_changed_line(pre, str(f)) == 4


def test_first_changed_line_tail_delete(tmp_path: Path) -> None:
    pre = b"a\nb\nc\nd\n"
    f = tmp_path / "f.txt"
    f.write_bytes(b"a\nb\n")
    # Shared prefix is 2 lines; divergence is the missing line 3.
    assert supertool._first_changed_line(pre, str(f)) == 3


def test_first_changed_line_no_change(tmp_path: Path) -> None:
    pre = b"a\nb\nc\n"
    f = tmp_path / "f.txt"
    f.write_bytes(pre)
    assert supertool._first_changed_line(pre, str(f)) is None


def test_first_changed_line_missing_file_returns_none(tmp_path: Path) -> None:
    assert supertool._first_changed_line(b"x\n", str(tmp_path / "nope.txt")) is None
