"""#945 — the two read ops reinterpret a number and say nothing about it.

`read:PATH:OFFSET:LIMIT` takes OFFSET as a *skip count*, so `read:f:19:1` renders
line 20 and the line the caller named is nowhere in the output. Nothing in the
render distinguishes "here is line 19" from "here is a line near 19", so a
caller quoting the result quotes the wrong line with full confidence. The same
render also claims `[complete file — no more lines]` after emitting a window
that skipped everything before OFFSET — and claims it after emitting *zero*
lines when OFFSET is past EOF.

`grep:PATTERN:PATH:LIMIT` accepts LIMIT 0, ignores it, and silently applies the
default. `0` is the near-universal spelling of "no limit", so the caller who
typed it believes they lifted the cap; what they get is neither that nor a
refusal.

Both pins fail on the pre-#945 code and neither would pass against a render
that merely produced *some* output.
"""

from __future__ import annotations

from pathlib import Path

import supertool


def _ten(tmp_path: Path) -> Path:
    f = tmp_path / "ten.txt"
    f.write_bytes(b"".join(b"line%d\n" % i for i in range(1, 11)))
    return f


# --- read: the returned window must be named, never merely rendered ---------


def test_read_offset_names_the_window_it_actually_returned(tmp_path: Path) -> None:
    """`read:f:5:1` renders line 6. Whatever the caller meant by `5`, the
    output has to state which lines it is, in the header, before the content."""
    f = _ten(tmp_path)
    out = supertool.dispatch(f"read:{f}:5:1")
    assert "     6→line6" in out
    window = [ln for ln in out.splitlines() if ln.startswith("window:")]
    assert window, f"no window disclosure in:\n{out}"
    assert "6-6" in window[0]
    assert "offset 5" in window[0]
    # ...and it must arrive before the content it describes, not after.
    assert out.index("window:") < out.index("line6")


def test_read_offset_disclosure_names_the_line_the_caller_asked_for(
    tmp_path: Path,
) -> None:
    """The reported case: `:19:1` meant "line 19" and returned line 20. The
    disclosure has to make the skip-count semantics visible and point at the
    range form, which is 1-based and inclusive."""
    f = tmp_path / "many.txt"
    f.write_bytes(b"".join(b"L%d\n" % i for i in range(1, 51)))
    out = supertool.dispatch(f"read:{f}:19:1")
    assert "    20→L20" in out
    assert "skip count" in out
    assert f"read:{f}:19-19" in out


def test_read_offset_past_eof_does_not_claim_a_complete_file(
    tmp_path: Path,
) -> None:
    """Zero lines emitted, and the old render answered `[complete file — no
    more lines]`. An absence produced by the arguments, rendered as the whole
    file having been shown."""
    f = _ten(tmp_path)
    out = supertool.dispatch(f"read:{f}:99:3")
    assert "[complete file" not in out
    assert "returning nothing" in out
    assert "10 lines" in out


def test_read_clamped_window_does_not_claim_a_complete_file(
    tmp_path: Path,
) -> None:
    """5 lines requested from offset 8, 2 returned, lines 1-8 never shown —
    and the old render called that a complete file."""
    f = _ten(tmp_path)
    out = supertool.dispatch(f"read:{f}:8:5")
    assert "     9→line9" in out
    assert "[complete file" not in out
    window = [ln for ln in out.splitlines() if ln.startswith("window:")]
    assert window, f"no window disclosure in:\n{out}"
    assert "9-13" in window[0]      # requested
    assert "9-10 of 10" in window[0]  # returned


def test_read_from_line_one_keeps_the_quiet_header(tmp_path: Path) -> None:
    """offset 0 cannot shift, clamp or overshoot, so it gets no window line —
    the disclosure is not a tax on the common read."""
    f = _ten(tmp_path)
    out = supertool.dispatch(f"read:{f}")
    assert "window:" not in out
    assert "[complete file — no more lines]" in out


# --- grep: LIMIT 0 is refused by name, not quietly redefined ----------------


def test_grep_limit_zero_is_refused_and_names_the_convention(
    tmp_path: Path,
) -> None:
    f = tmp_path / "hay.txt"
    f.write_bytes(b"needle\n" * 40)
    out = supertool.dispatch(f"grep:needle:{f}:0")
    assert out.startswith("ERROR:") or "\nERROR:" in out
    assert "unlimited" in out
    assert "needle" not in out.replace(f"grep:needle:{f}:0", "")


def test_grep_limit_zero_with_context_is_refused_before_the_bytes(
    tmp_path: Path,
) -> None:
    """The filed case — `:0:200` spent ~18KB before the caller could see that
    `0` had not meant what they typed it to mean."""
    f = tmp_path / "hay.txt"
    f.write_bytes(b"".join(b"needle %d\n" % i for i in range(400)))
    out = supertool.dispatch(f"grep:needle:{f}:0:200")
    assert "ERROR:" in out
    assert len(out) < 1200, f"refusal should be cheap, got {len(out)} bytes"


def test_grep_positive_limit_still_works(tmp_path: Path) -> None:
    f = tmp_path / "hay.txt"
    f.write_bytes(b"needle\n" * 40)
    out = supertool.dispatch(f"grep:needle:{f}:3")
    assert "ERROR:" not in out


def test_grep_omitted_limit_still_defaults(tmp_path: Path) -> None:
    f = tmp_path / "hay.txt"
    f.write_bytes(b"needle\n" * 40)
    out = supertool.dispatch(f"grep:needle:{f}")
    assert "ERROR:" not in out


def test_grep_around_limit_zero_is_refused_too(tmp_path: Path) -> None:
    """`grep_around:PATTERN:PATH:N:LIMIT` reaches the same op with the same
    argument and had the same silent redefinition."""
    f = tmp_path / "hay.txt"
    f.write_bytes(b"needle\n" * 40)
    out = supertool.dispatch(f"grep_around:needle:{f}:2:0")
    assert "ERROR:" in out
    assert "unlimited" in out


def test_grep_payload_limit_zero_is_refused(tmp_path: Path) -> None:
    payload = tmp_path / "g.toml"
    hay = tmp_path / "hay.txt"
    hay.write_bytes(b"needle\n" * 40)
    payload.write_text(
        'pattern = "needle"\n'
        f'path = "{hay}"\n'
        "limit = 0\n"
    )
    out = supertool.dispatch(f"grep:@{payload}")
    assert "ERROR:" in out
    assert "unlimited" in out
