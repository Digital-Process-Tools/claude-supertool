"""A full-file `read:PATH` delegated to `rtk read` came back garbled (#1786).

Reproduced directly against the real `rtk` binary (github.com/reachingforthejack/rtk,
0.35.0): `rtk read -n --max-lines N FILE` on a file over its window renders the
file's real last line sandwiched between TWO elision-style footers whose
counts do not even sum to the file's own line count --

    149 | x148 = 148  # line 148
        // ... 362 lines omitted
    export = 1
    // ... 361 more lines (total: 512)

`grep`-ing this module for "lines omitted" finds nothing: supertool never
emits that phrase, so this is a bug in `rtk`'s own compression, not in
anything `_supertool.py` computes. What IS this module's own bug is that
`render_file`'s RTK-delegation branch trusted that render wholesale and
returned it to the caller unchanged -- a caller who does not already know
the file cannot tell a garbled compression from a correct one, which is the
exact failure mode #1786 named as the worst part of the three it reported.

Every "must fall back" case here is paired with a "must still delegate"
positive control in the same fixture (`test_a_single_elision_marker_is_left_alone`),
so a change that broke delegation entirely could not pass this file by making
everything fall back.
"""

from __future__ import annotations

from pathlib import Path

import supertool

NL = chr(10)


def _many_lines(tmp_path: Path, n: int, name: str = "many.txt") -> Path:
    f = tmp_path / name
    f.write_text(NL.join(f"L{i}" for i in range(1, n + 1)) + NL, encoding="utf-8")
    return f


def test_rtk_output_looks_malformed_detects_the_reproduced_shape() -> None:
    """The exact bytes reproduced against the real `rtk` binary."""
    garbled = (
        "149 | x148 = 148  # line 148" + NL
        + "    // ... 362 lines omitted" + NL
        + "export = 1" + NL
        + "// ... 361 more lines (total: 512)" + NL
    )
    assert supertool._rtk_output_looks_malformed(garbled), garbled


def test_a_single_elision_marker_is_left_alone() -> None:
    """Positive control: rtk's ordinary, well-formed truncation footer."""
    ordinary = "  1 | L1" + NL + "// ... 999 more lines" + NL
    assert not supertool._rtk_output_looks_malformed(ordinary), ordinary


def test_delegated_read_falls_back_and_discloses_on_malformed_rtk_output(
    tmp_path: Path, monkeypatch
) -> None:
    """The branch that actually fires: `read:PATH` must not hand the caller
    rtk's garbled render, and must say it fell back."""
    f = _many_lines(tmp_path, 1000)
    monkeypatch.setattr(supertool, "_rtk_enabled", lambda: True)
    monkeypatch.setattr(supertool, "_has_rtk", lambda: True)
    monkeypatch.setattr(
        supertool, "_rtk_run",
        lambda args, timeout=30: (
            "149 | x148" + NL
            + "    // ... 362 lines omitted" + NL
            + "export = 1" + NL
            + "// ... 361 more lines (total: 512)" + NL
        ))
    out = supertool.dispatch(f"read:{f}")
    assert "362 lines omitted" not in out, (
        "rtk's malformed render must never reach the caller unchanged:" + NL + out)
    assert "malformed" in out and "#1786" in out, (
        "the fallback must be disclosed, not silently swapped in:" + NL + out)
    assert "L1" in out, (
        "the native renderer must still have produced real content:" + NL + out)


def test_delegated_read_of_well_formed_rtk_output_is_unaffected(
    tmp_path: Path, monkeypatch
) -> None:
    """Positive control for the delegated branch: an ordinary rtk render
    must still be returned as-is, with no fallback note."""
    f = _many_lines(tmp_path, 1000)
    monkeypatch.setattr(supertool, "_rtk_enabled", lambda: True)
    monkeypatch.setattr(supertool, "_has_rtk", lambda: True)
    monkeypatch.setattr(
        supertool, "_rtk_run",
        lambda args, timeout=30: "  1 | L1" + NL + "// ... 999 more lines" + NL)
    out = supertool.dispatch(f"read:{f}")
    assert "L1" in out, out
    assert "malformed" not in out, out
