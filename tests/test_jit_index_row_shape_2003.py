r"""The tools-row shape check accepts 4+ fields while its refusal says 6 or 7 (#2003).

`validators/jit-index/jit-index.py:153`'s row-shape test was `len(fields) >= 4`,
so a hand-typed row missing a column -- 4 or 5 fields, or one with a stray extra
tab pushing it to 9 -- was silently accepted as a tools row. The refusal message
two branches down (`:171-172`) already said a tools row has "6 or 7" fields
(claude-jit-context 0.6.0 added a 7th `requires` column, #1992), so the
predicate and its own error message disagreed, and #1992 corrected only the
prose.

The consequence is the one CLAUDE.md names in as many words: a hand-typed row
missing a column passes clean, the hook then reads `mode` out of the wrong
column, and a `block` rule silently stops blocking. A rule that never matches
and a rule that never runs render identically.

This pins the exercise table from #2003 directly: the two legal field counts
(6 and 7) must be accepted with zero shape errors, and three illegal ones in
between and around them (4, 5, 9) must each be refused as a shape error that
actually fails the validator (`ok: False`) -- a shape error that does not flip
`ok` would be the same defect one layer up.

Would this pass if the code did nothing? No: today's `>= 4` predicate accepts
4, 5 and 9 fields with `ok: True` (and no way for the caller to notice the
row was ill-formed) -- exactly the shape this test refuses to let through.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ADAPTER = REPO / "validators" / "jit-index" / "jit-index.py"

TAB = "\t"


def _run(target):
    proc = subprocess.run(
        [sys.executable, str(ADAPTER), str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert proc.stdout.strip(), "adapter emitted nothing (stderr: {0})".format(proc.stderr)
    return json.loads(proc.stdout)


def _index(tmp_path, *rows):
    d = tmp_path / "jit-context" / "tools" / "00-manual"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "00-index.tsv"
    f.write_text("".join(r + "\n" for r in rows), encoding="utf-8")
    return f


def _fields(n):
    """A well-formed field 1 (`~pattern`) padded to exactly n tab-separated fields."""
    row = ["Bash", "~gh push"] + ["x"] * (n - 2)
    return TAB.join(row[:n]) if n >= 2 else TAB.join(row)


def _shape_codes(result):
    return [e["code"] for e in result.get("errors", [])]


class TestLegalFieldCounts:
    """The two shapes claude-jit-context actually writes."""

    def test_six_fields_is_accepted_with_no_shape_error(self, tmp_path):
        idx = _index(tmp_path, _fields(6))
        result = _run(idx)
        assert "shape" not in _shape_codes(result)

    def test_seven_fields_is_accepted_with_no_shape_error(self, tmp_path):
        """claude-jit-context 0.6.0 added a 7th `requires` column (#1992)."""
        idx = _index(tmp_path, _fields(7))
        result = _run(idx)
        assert "shape" not in _shape_codes(result)


class TestIllegalFieldCounts:
    """Between and around the two legal counts -- the table in #2003."""

    @pytest.mark.parametrize("n", [4, 5, 9])
    def test_is_refused_as_a_shape_error(self, tmp_path, n):
        idx = _index(tmp_path, _fields(n))
        result = _run(idx)
        assert result["ok"] is False, (
            "a {0}-field tools row must be refused, not silently accepted".format(n))
        assert "shape" in _shape_codes(result)

    @pytest.mark.parametrize("n", [4, 5, 9])
    def test_the_refusal_names_the_actual_field_count(self, tmp_path, n):
        idx = _index(tmp_path, _fields(n))
        result = _run(idx)
        msgs = " | ".join(e["msg"] for e in result["errors"])
        assert "{0} tab-separated field".format(n) in msgs
