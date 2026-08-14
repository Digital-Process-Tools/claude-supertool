"""map must not report a file it could not open as a file with no symbols (#1680).

`chmod 000 secret.py && supertool 'map:secret.py'` printed three unearned claims:
`tier: regex` for a tier that read no bytes, `(0 lines)` from `_count_lines`
swallowing the `OSError`, and `(no symbols)` - the tool's own failure stated as a
property of the file. It sat inside the #887 render built to make exactly that
shape visible.

The gate is established once per file, ahead of `_count_lines`, `_ts_extract` and
`_regex_extract`, rather than in each of the three: `_count_lines`'s `on_error`
contract exists because its callers want opposite sentinels (#388), so a
composing render cannot tell `0 lines because empty` from `0 lines because
unreadable` - that distinction was destroyed by design one layer down.

Two arms, and neither branches on `os.name`: a file whose mode denies reading,
skipped only when the running process can read it anyway (root, or a filesystem
that ignores the mode), and a file that disappears between the walk and the read,
which needs no permissions at all.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import supertool

from _changelog_findable import assert_change_is_findable


def _unreadable(tmp_path: Path) -> Path:
    f = tmp_path / "secret.py"
    f.write_text("def visible():\n    pass\n")
    os.chmod(f, 0o000)
    return f


def _can_still_read(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            fh.read(1)
        return True
    except OSError:
        return False


def _needs_denial(path: Path) -> None:
    if _can_still_read(path):
        pytest.skip("this process can read a 0o000 file, so the arm cannot be posed here")


def test_a_file_the_tool_could_not_open_is_not_a_file_without_symbols(tmp_path: Path) -> None:
    f = _unreadable(tmp_path)
    _needs_denial(f)

    out = supertool.op_map(str(f))

    assert "(no symbols)" not in out, out
    assert "(0 lines)" not in out, out
    assert "could not read" in out, out


def test_the_tier_line_never_names_a_tier_that_read_no_bytes(tmp_path: Path) -> None:
    f = _unreadable(tmp_path)
    _needs_denial(f)

    out = supertool.op_map(str(f))

    assert "tier: regex" not in out, out
    assert "tier: none" in out, out


def test_a_file_that_vanished_between_the_walk_and_the_read_declines(tmp_path: Path) -> None:
    """No permissions involved: the walk listed it, the read found nothing there."""
    gone = tmp_path / "gone.py"
    with patch.object(supertool, "_collect_files", return_value=[str(gone)]):
        out = supertool.op_map(str(tmp_path))

    assert "(no symbols)" not in out, out
    assert "could not read" in out, out


def test_a_readable_file_is_unaffected(tmp_path: Path) -> None:
    f = tmp_path / "plain.py"
    f.write_text("def thing():\n    pass\n")

    out = supertool.op_map(str(f))

    assert "could not read" not in out, out
    assert "thing" in out, out


def test_the_abstract_read_does_not_relabel_unreadable_as_no_symbols(tmp_path: Path) -> None:
    """`read:` falls back to raw source, and must say WHICH failure it hit.

    `_abstract_map` gained the new marker as a fallback trigger and kept one
    reason string for all of them, so an unreadable file came back as `no
    symbols found in X (python)` — plus, with no tree-sitter installed, a
    footnote about which tier ran. That is #1680 again one layer up, in the
    caller. Raised by the independent review of the first commit.
    """
    f = _unreadable(tmp_path)
    _needs_denial(f)

    body, reason = supertool._abstract_map(str(f), "python", 4096)

    assert body == "", body
    assert "no symbols found" not in reason, reason
    assert "could not read" in reason, reason


def test_change_is_documented() -> None:
    assert_change_is_findable("1680", Path(__file__).resolve().parent.parent)
