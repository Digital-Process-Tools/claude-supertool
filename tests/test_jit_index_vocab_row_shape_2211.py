r"""jit-index accepts the 3-column vocabulary row claude-jit-context writes (#2211).

`rebuild-tsv.sh`'s `build_vocab_tsv` (claude-jit-context 0.7.1) writes
`keyword<TAB>file<TAB>verdict` for every vocabulary row -- `verdict` is either
empty or the literal word `generic`. Before this fix, `validators/jit-index/
jit-index.py`'s `_rows()` only knew a tools shape (6 or 7 fields) and a paths
shape (2 fields); a 3-field vocabulary row fell into the catch-all branch and
was refused as a shape error, so an edit that regenerated a vocabulary index
for real -- three columns, as claude-jit-context actually writes them -- could
never land through this validator.

Column 1 of a vocabulary row is never handed to awk: `pre-prompt-hook.sh`
matches a keyword with a literal `index()` against a padded prompt, not
`match()`. So a vocabulary row must be accepted with zero shape errors AND
without being pulled into the awk pattern-compile checks that a paths row's
first column goes through.

Would this pass if the code did nothing? No: before this fix every one of
these three-field rows was refused with a shape finding.
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
    d = tmp_path / "jit-context" / "vocabulary" / "00-manual"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "00-index.tsv"
    f.write_text("".join(r + "\n" for r in rows), encoding="utf-8")
    return f


def _shape_codes(result):
    return [e["code"] for e in result.get("errors", [])]


@pytest.mark.parametrize("verdict", ["", "generic"])
def test_three_field_vocab_row_is_accepted_with_no_shape_error(tmp_path, verdict):
    row = TAB.join(["three states", "three-states.md", verdict])
    idx = _index(tmp_path, row)
    result = _run(idx)
    assert "shape" not in _shape_codes(result), result


def test_three_field_row_with_an_unknown_verdict_is_refused(tmp_path):
    row = TAB.join(["three states", "three-states.md", "bogus"])
    idx = _index(tmp_path, row)
    result = _run(idx)
    assert result["ok"] is False
    assert "shape" in _shape_codes(result)


def test_three_field_row_with_an_empty_keyword_is_refused(tmp_path):
    row = TAB.join(["", "three-states.md", ""])
    idx = _index(tmp_path, row)
    result = _run(idx)
    assert result["ok"] is False
    assert "shape" in _shape_codes(result)


def test_vocab_keyword_column_is_not_treated_as_an_awk_pattern(tmp_path):
    """A keyword containing \\s must not be flagged as a dead awk escape --
    it is matched with a literal index(), never compiled by awk."""
    row = TAB.join(["gh\\spr", "three-states.md", ""])
    idx = _index(tmp_path, row)
    result = _run(idx)
    assert result["ok"] is True, result
    assert result.get("errors", []) == []
