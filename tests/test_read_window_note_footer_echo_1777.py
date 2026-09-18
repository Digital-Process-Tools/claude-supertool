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

Gated on `FOOTER_ECHO_MIN_LINES` (#1777, found by CI after this PR first
opened): `tests/test_hint_points_at_the_fix_1489.py` and
`tests/test_read_range_382.py` already pin "one disclosure, above the body"
for a 48-printed-line window with a documented reason (#1489: a trailing
note the reader reaches after already paying for the wrong window is barely
a note) -- the same argument this PR's own issue makes about a much LARGER
window scrolling the header out of view before it is even read once. Both
are true, at different sizes: small enough to read start-to-finish in one
screen, the header is enough; large enough to force scrolling past content
the header already warned about, the header alone is not. The unconditional
version broke both existing tests on CI, which is what this gate fixes.
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
    # A single-element list makes `windows[0] == windows[-1]` trivially true
    # by comparing one line to itself -- this would pass on the unpatched
    # code, which emits exactly one `window:` line (review finding, #1777).
    assert len(windows) >= 2, (
        "this assertion is meaningless without two lines to compare: "
        + repr(out))
    assert windows[0] == windows[-1], (
        "the footer must say the same thing as the header, not a shortened "
        "or divergent restatement: " + repr(out))


def test_a_small_window_keeps_the_established_one_disclosure_shape(
        tmp_path: Path) -> None:
    """A short window (offset > 0) keeps the #1489/#382 shape -- one
    disclosure, above the body -- unchanged. The footer echo is gated on
    window size (`FOOTER_ECHO_MIN_LINES`), not merely on offset > 0: an
    unconditional footer here is exactly what broke
    tests/test_hint_points_at_the_fix_1489.py and
    tests/test_read_range_382.py on CI."""
    f = _many(tmp_path, 10)
    out = supertool.dispatch(f"read:{f}:5:1")
    windows = [ln for ln in out.splitlines() if ln.startswith("window:")]
    assert len(windows) == 1, repr(out)


def test_the_footer_echo_gate_boundary(tmp_path: Path) -> None:
    """Pin the threshold itself: exactly `FOOTER_ECHO_MIN_LINES` printed
    lines still gets one copy (the gate is strictly-greater-than); one line
    more gets two. `offset=1` with a file well past `limit + 1` keeps
    `printed == limit` in both calls, so the only variable is the boundary
    itself."""
    import _supertool as core

    threshold = core.FOOTER_ECHO_MIN_LINES
    f = _many(tmp_path, threshold + 50)

    at_threshold = supertool.dispatch(f"read:{f}:1:{threshold}")
    windows_at = [ln for ln in at_threshold.splitlines()
                  if ln.startswith("window:")]
    assert len(windows_at) == 1, (
        f"exactly {threshold} printed lines must NOT clear a "
        f"strictly-greater-than gate: " + repr(at_threshold))

    just_over = supertool.dispatch(f"read:{f}:1:{threshold + 1}")
    windows_over = [ln for ln in just_over.splitlines()
                    if ln.startswith("window:")]
    assert len(windows_over) == 2, (
        f"{threshold + 1} printed lines must clear the gate and get the "
        f"footer echo: " + repr(just_over))


def test_no_footer_echo_when_offset_is_zero(tmp_path: Path) -> None:
    """`read:PATH` with no offset never triggered the header note either --
    the footer echo must not appear where the header note would not."""
    f = _many(tmp_path, 10)
    out = supertool.dispatch(f"read:{f}")
    windows = [ln for ln in out.splitlines() if ln.startswith("window:")]
    assert len(windows) == 0, repr(out)
