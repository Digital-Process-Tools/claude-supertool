"""`_count_lines` was defined twice at module scope (#388).

Python binds the last definition, so the auto-read line cap — the caller the
first one was written for — silently reached the second, whose `OSError → 0`
inverted the documented contract: a file that could not be measured counted as
*under* the cap and was auto-read, when the whole point was to skip it.

The callers genuinely want opposite things, so the error value is a parameter
now rather than a constant somebody has to guess right.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import supertool


def test_counts_lines(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_bytes(b"a\nb\nc\n")
    assert supertool._count_lines(str(f)) == 3


def test_counts_a_trailing_line_without_a_newline(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_bytes(b"a\nb\nc")
    assert supertool._count_lines(str(f)) == 3


def test_empty_file_is_zero(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_bytes(b"")
    assert supertool._count_lines(str(f)) == 0


def test_counts_across_the_chunk_boundary(tmp_path: Path) -> None:
    """The reader streams in 64KB chunks; the count must not reset per chunk."""
    f = tmp_path / "big.txt"
    f.write_bytes(b"x" * 70000 + b"\n" + b"y\n")
    assert supertool._count_lines(str(f)) == 2


def test_unreadable_defaults_to_zero() -> None:
    """A listing wants a truthful 0, not a sentinel rendered as a line count."""
    assert supertool._count_lines("/nope/does/not/exist") == 0


def test_unreadable_can_fail_closed() -> None:
    """The auto-read cap needs the opposite answer: treat it as over-cap."""
    sentinel = supertool.MAX_AUTOREAD_LINES + 1
    assert supertool._count_lines("/nope/does/not/exist", on_error=sentinel) == sentinel


def test_only_one_definition_survives() -> None:
    """The bug was a second module-scope def silently shadowing the first."""
    import inspect
    src = inspect.getsource(supertool)
    assert src.count("\ndef _count_lines(") == 1


@pytest.mark.skipif(os.name == "nt", reason="chmod 000 is not a read barrier on Windows")
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads regardless of mode")
def test_permission_denied_uses_the_error_value(tmp_path: Path) -> None:
    f = tmp_path / "secret.txt"
    f.write_bytes(b"a\nb\n")
    f.chmod(0o000)
    try:
        assert supertool._count_lines(str(f)) == 0
        assert supertool._count_lines(str(f), on_error=999) == 999
    finally:
        f.chmod(0o644)
