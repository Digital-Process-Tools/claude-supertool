"""A full-file `read:PATH` delegated to `rtk read` came back garbled (#1786).

Reproduced directly against the real `rtk` binary (github.com/rtk-ai/rtk,
0.35.0): `rtk read -n --max-lines N FILE` on a file over its window renders the
file's real last line sandwiched between TWO elision-style footers whose
counts do not even sum to the file's own line count -- e.g. (real bytes,
`repr()`'d so the box-drawing separator is visible rather than swallowed)::

    '151 │     // ... 362 lines omitted\n'
    '152 │ export = 1\n'
    '153 │ // ... 361 more lines (total: 512)\n'

`grep`-ing this module for "lines omitted" finds nothing: supertool never
emits that phrase, so this is a bug in `rtk`'s own compression, not in
anything `_supertool.py` computes. What IS this module's own bug is that
`render_file`'s RTK-delegation branch trusted that render wholesale and
returned it to the caller unchanged -- a caller who does not already know
the file cannot tell a garbled compression from a correct one, which is the
exact failure mode #1786 named as the worst part of the three it reported.

Every "must fall back" case here is paired with a "must still delegate"
positive control in the same fixture, so a change that broke delegation
entirely could not pass this file by making everything fall back.

Self-review found two more shapes a bare substring match cannot tell from
the corruption above, both fixed in the same pass as this test file:

* `--level aggressive` legitimately renders SEVERAL elision markers in one
  correct output, one per compacted function body
  (`test_rtk_aggressive_multi_block_elision_is_not_malformed`).
* Ordinary prose that merely QUOTES the marker shape -- this very docstring,
  or the changelog fragment for #1786 -- is not rtk's own footer at all
  (`test_prose_that_merely_quotes_the_marker_shape_is_not_malformed`).
  `_RTK_ELISION_MARKER_RE` is anchored to the whole-line shape `rtk -n`
  actually emits (a line-number, its `│` column separator, then nothing
  but the marker), which the ASCII `|` used for readability in this
  docstring's example above does not carry -- confirmed directly against the
  real binary reading this very file: rtk renders all of it, since it is
  well under the 300-line window, and neither the malformed nor the
  false-positive detector fires on that clean render.
"""

from __future__ import annotations

from pathlib import Path

import supertool

NL = chr(10)
SEP = chr(0x2502)


def _many_lines(tmp_path: Path, n: int, name: str = "many.txt") -> Path:
    f = tmp_path / name
    f.write_text(NL.join(f"L{i}" for i in range(1, n + 1)) + NL, encoding="utf-8")
    return f


def test_rtk_output_looks_malformed_detects_the_reproduced_shape() -> None:
    """The exact bytes reproduced against the real `rtk` binary, byte for
    byte -- including its `│` column separator, never a plain `|`."""
    garbled = (
        "149 " + SEP + " x148 = 148  # line 148" + NL
        + "150 " + SEP + " x149 = 149  # line 149" + NL
        + "151 " + SEP + "     // ... 362 lines omitted" + NL
        + "152 " + SEP + " export = 1" + NL
        + "153 " + SEP + " // ... 361 more lines (total: 512)" + NL
    )
    assert supertool._rtk_output_looks_malformed(garbled), garbled


def test_a_single_elision_marker_is_left_alone() -> None:
    """Positive control: rtk's ordinary, well-formed truncation footer."""
    ordinary = "  1 " + SEP + " L1" + NL + "300 " + SEP + " // ... 999 more lines" + NL
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
            "149 " + SEP + " x148" + NL
            + "150 " + SEP + "     // ... 362 lines omitted" + NL
            + "151 " + SEP + " export = 1" + NL
            + "152 " + SEP + " // ... 361 more lines (total: 512)" + NL
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
        lambda args, timeout=30: (
            "  1 " + SEP + " L1" + NL + "300 " + SEP + " // ... 999 more lines" + NL
        ))
    out = supertool.dispatch(f"read:{f}")
    assert "L1" in out, out
    assert "malformed" not in out, out


def test_rtk_aggressive_multi_block_elision_is_not_malformed() -> None:
    """A REAL rtk render, reproduced byte for byte against rtk 0.35.0's
    `--level aggressive` on a multi-function file: each compacted function
    body gets its OWN `// ... N lines omitted` marker, and the final line is
    a genuine `// ... N more lines (total: M)` summary. Four elision markers
    in one render here are all legitimate -- this is not a truncation
    cutting once, it is per-block compaction cutting several times on
    purpose. Treating this the same as the two-marker corruption (#1786)
    would discard a correct compact render and silently defeat the point of
    RTK delegation for exactly the caller who asked for compact output.
    """
    real_aggressive_render = (
        " 1 " + SEP + " import json, sys" + NL
        + " 2 " + SEP + " def func_1():" + NL
        + " 3 " + SEP + "     // ... implementation" + NL
        + " 4 " + SEP + " def func_2():" + NL
        + " 5 " + SEP + "     // ... implementation" + NL
        + " 6 " + SEP + " def func_3():" + NL
        + " 7 " + SEP + "     // ... 13 lines omitted" + NL
        + " 8 " + SEP + " def func_4():" + NL
        + " 9 " + SEP + "     // ... 12 lines omitted" + NL
        + "10 " + SEP + " def func_5():" + NL
        + "11 " + SEP + "     // ... 11 lines omitted" + NL
        + "12 " + SEP + " def func_6():" + NL
        + "13 " + SEP + " // ... 10 more lines (total: 19)" + NL
    )
    assert not supertool._rtk_output_looks_malformed(
        real_aggressive_render, aggressive=True
    ), real_aggressive_render


def test_the_same_double_marker_shape_is_still_malformed_at_default_level() -> None:
    """Positive control: the exact corruption reproduced in #1786 is at
    `--level` none/minimal, never `--level aggressive` -- it must still be
    caught when the call was not made with `aggressive=True`."""
    garbled = (
        "149 " + SEP + " x148 = 148  # line 148" + NL
        + "151 " + SEP + "     // ... 362 lines omitted" + NL
        + "152 " + SEP + " export = 1" + NL
        + "153 " + SEP + " // ... 361 more lines (total: 512)" + NL
    )
    assert supertool._rtk_output_looks_malformed(garbled, aggressive=False), garbled


def test_prose_that_merely_quotes_the_marker_shape_is_not_malformed() -> None:
    """A file discussing THIS bug -- this very test file, or the changelog
    fragment for #1786 -- can legitimately contain the marker text as
    ordinary quoted prose, with no real rtk footer around it (self-review;
    confirmed against the real rtk 0.35.0 binary reading this very file: a
    file well under rtk's 300-line window renders whole, with the marker
    text appearing only inside its own docstring, and neither this detector
    nor the earlier substring-only version -- checked by hand against the
    committed diff before this fix -- may fire on it). A substring match
    with no anchor to rtk's actual footer shape cannot tell that apart from
    a genuine corrupted render, and would falsely tell a caller a perfectly
    clean file "looked malformed" -- found by `oss:auditor` self-review
    against this exact file and this fix's own changelog fragment, both of
    which quote the marker text as ordinary prose with a plain `|`, never
    rtk's real separator. (Named without its literal path here on purpose --
    tests/test_changelog_findable_1293.py refuses any tracked file that
    names a still-pending changelog.d fragment by its literal path, since
    the tag that ships this fix deletes it and the reference would go red on
    every release after.)
    """
    prose = (
        "1 | some line" + NL
        + "2 | this docstring talks about the // ... 362 lines omitted shape" + NL
        + "3 | and also the // ... 361 more lines (total: 512) shape" + NL
        + "4 | end" + NL
    )
    assert not supertool._rtk_output_looks_malformed(prose), prose
