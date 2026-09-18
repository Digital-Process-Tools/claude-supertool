"""The OFFSET/LIMIT correction sits only above a long window and scrolls off (#1777).

`read:PATH:195:300` silently means OFFSET:LIMIT, and the disclosure that says
so -- which form was read, how many lines were skipped, and what to type for
the other form -- is excellent, but it sits above the file content. With a
189-line window the correction scrolls off and a caller reading only the tail
of the output sees the requested lines with no visible correction at all.

`between:PATH:200:300` gets this right the other way: it refuses outright and
names the fix. `read` keeps the silent OFFSET:LIMIT reading (breaking it would
break every caller who already has it right), so the fix here is to repeat
the same window note at the foot of the output as well as the head -- a
caller who only reads the end of a long window still sees it.
"""
from __future__ import annotations

from pathlib import Path

import supertool


def _many(tmp_path: Path, n: int) -> Path:
    f = tmp_path / "many.txt"
    f.write_bytes(b"".join(b"L%d\n" % i for i in range(1, n + 1)))
    return f


def test_the_window_note_is_repeated_at_the_foot_of_a_long_window(
        tmp_path: Path) -> None:
    f = _many(tmp_path, 500)
    out = supertool.dispatch(f"read:{f}:195:300")
    windows = [ln for ln in out.splitlines() if ln.startswith("window:")]
    assert len(windows) >= 2, (
        "a correction that sits only above a long window scrolls off before "
        "the caller reads the lines it corrects: " + repr(out))
    # ...and the footer copy must arrive after the content, not merely be a
    # second copy stacked with the first at the head.
    last_content_line = out.rindex("L494")
    assert out.rindex("window:") > last_content_line, (
        "the footer echo has to follow the content it corrects, not precede "
        "it a second time: " + repr(out))


def test_the_footer_echo_carries_the_same_facts_as_the_header(
        tmp_path: Path) -> None:
    f = _many(tmp_path, 500)
    out = supertool.dispatch(f"read:{f}:195:300")
    windows = [ln for ln in out.splitlines() if ln.startswith("window:")]
    assert windows[0] == windows[-1], (
        "the footer must say the same thing as the header, not a shortened "
        "or divergent restatement: " + repr(out))


def test_a_small_window_still_gets_no_duplicate_content_confusion(
        tmp_path: Path) -> None:
    """A short window (offset > 0) still gets exactly the header-plus-footer
    shape -- this is not gated on window length, only on offset > 0."""
    f = _many(tmp_path, 10)
    out = supertool.dispatch(f"read:{f}:5:1")
    windows = [ln for ln in out.splitlines() if ln.startswith("window:")]
    assert len(windows) == 2, repr(out)


def test_no_footer_echo_when_offset_is_zero(tmp_path: Path) -> None:
    """`read:PATH` with no offset never triggered the header note either --
    the footer echo must not appear where the header note would not."""
    f = _many(tmp_path, 10)
    out = supertool.dispatch(f"read:{f}")
    windows = [ln for ln in out.splitlines() if ln.startswith("window:")]
    assert len(windows) == 0, repr(out)
