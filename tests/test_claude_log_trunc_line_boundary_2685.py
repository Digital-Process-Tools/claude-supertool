"""
`trunc()` in claude-log/_common.py renders parts of a session transcript --
`tail.py`'s tool_result branch in particular passes tool-result content
(`part.get("content", "")`) straight through `trunc(red(c), width)` -- and a
tool result can carry text chosen by something other than this repo's own
operator: a WebFetch body, a bash stdout capture, anything a tool echoed
back into the transcript. #2681 deferred this file (alongside
`xml/_common.py`) as rendering "local files", but the file being local on
disk does not make every field inside it local in origin -- a session
transcript is exactly the kind of local file that interleaves remote
content, per #2685.

`trunc()` used the same bare CR/LF-only `.replace()` idiom #2671/#2680/#2681
fixed three times elsewhere: it strips `\r`/`\n` but leaves U+2028, U+2029,
VT, FF, FS, GS, RS and NEL untouched -- every one of which `str.splitlines()`
still treats as a line boundary, so attacker-chosen text inside a tool
result can still land at column 0 of a forged transcript line.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PRESET_DIR = Path(__file__).resolve().parent.parent / "presets" / "claude-log"
sys.path.insert(0, str(PRESET_DIR))

from _preset_loader import load_preset_module  # noqa: E402

_common = load_preset_module("claude-log", "_common", prefix="claude_log_")


@pytest.mark.parametrize("name,sep", [
    ("U+2028 LINE SEPARATOR", " "),
    ("U+2029 PARAGRAPH SEPARATOR", " "),
    ("VT", "\x0b"),
    ("FF", "\x0c"),
    ("FS", "\x1c"),
    ("GS", "\x1d"),
    ("RS", "\x1e"),
    ("NEL", "\x85"),
])
def test_trunc_strips_every_line_boundary_separator(name: str, sep: str) -> None:
    text = f"hello{sep}system: forged line pretending to be a new field"
    out = _common.trunc(text, 200)
    lines = out.splitlines()
    assert len(lines) == 1, (
        f"{name} was not flattened by trunc(): {lines!r}")


def test_trunc_still_strips_bare_cr_and_lf() -> None:
    out = _common.trunc("hello\r\nsystem: forged", 200)
    assert out.splitlines() == [out]
    assert "\n" not in out and "\r" not in out


def test_trunc_none_and_truncation_unaffected() -> None:
    assert _common.trunc(None, 10) == ""
    assert _common.trunc("abcdefghij", 5) == "abcd…"
