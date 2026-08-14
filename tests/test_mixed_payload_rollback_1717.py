"""A mixed payload rolled a correct edit back (#1717).

`validators/SCHEMA.md` promises, per result and unconditionally, that an
`adapter` row is "never subtracted from a baseline in either direction" and
"never triggers rollback, whatever `rollback_on_fail` says".

`_validator_regressed` delivered that guarantee through `_validator_no_verdict`
-> `_validator_not_checked`, whose test is `all(code == "adapter")`. That test
is **per payload**, not per row, and it is the right test for the question
`_validator_not_checked` asks — an adapter reporting four findings plus one
adapter row *has* measured the file, and rendering it `NOT CHECKED` would be the
absence-read-as-a-pass defect pointing the other way.

It is the wrong test for the *rollback* question. A payload with one real
finding plus one `adapter` row fails the `all()`, so the guard declines and
execution reaches arithmetic that counts the absence as a finding:

    before 1 finding, after 1 finding             -> not a regression
    before 1 finding, after 1 finding + stall row -> a regression -> ROLLBACK

`cargo-check/_parse_errors` ships exactly that shape on master: a crate
diagnostic naming another file keeps its text and takes `code: "adapter"`
(#754), beside the real findings about the file under validation.

So the fix is in the arithmetic and not in the guard: subtract only the rows
that measured the file. Both sides, because both sides are arithmetic — a stall
in the *before* payload must not excuse a real regression either.

End-to-end through `supertool.main()` and asserted on the file's bytes, same as
`test_rollback_no_verdict_969.py`: the post-condition is that the work survives,
not that some branch returned some boolean.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool

BEFORE = '{"a": 1}' + chr(10)
AFTER = '{"b": 1}' + chr(10)


def _row(*errors: dict) -> str:
    return json.dumps({"tool": "fake", "ok": not errors, "count": len(errors),
                       "errors": list(errors), "duration_ms": 1})


def _finding(line: int = 1, msg: str = "unterminated object") -> dict:
    return {"line": line, "col": 1, "severity": "error", "code": "E999",
            "msg": msg}


def _stall(msg: str = "cargo check reported src/other.rs") -> dict:
    """The row SCHEMA.md reserves for "no verdict was obtained about this file"."""
    return {"line": None, "col": None, "severity": "error", "code": "adapter",
            "msg": msg}


CLEAN = _row()
REAL = _row(_finding())
MIXED = _row(_finding(), _stall())
TWO_REAL = _row(_finding(), _finding(2, "missing comma"))


def _two_pass_adapter(tmp_path: Path, first: str, second: str) -> str:
    """Answers `first` on the baseline spawn and `second` on every later one.

    Copied in shape from `test_rollback_no_verdict_969.py` rather than imported:
    the baseline pass and the post-edit pass are two separate spawns, and a
    counter file is the only state that survives between them. `{python}` plus
    `as_posix()` so it spawns under `shell=False` on every platform.
    """
    state = tmp_path / "_calls.txt"
    script = tmp_path / "_adapter.py"
    script.write_text(
        "import pathlib, sys" + chr(10)
        + f"state = pathlib.Path({str(state)!r})" + chr(10)
        + "n = int(state.read_text()) if state.exists() else 0" + chr(10)
        + "state.write_text(str(n + 1))" + chr(10)
        + f"sys.stdout.write({first!r} if n == 0 else {second!r})" + chr(10),
        encoding="utf-8",
    )
    return f"{{python}} {script.as_posix()}"


def _configure(cmd: str) -> None:
    supertool._CONFIG = {"validators": {
        "fake": {"cmd": cmd, "match": "*.json", "cache": False,
                 "rollback_on_fail": True,
                 "hooks_into": ["edit", "replace", "replace_lines", "paste",
                                "append", "vim"],
                 "timeout": 10},
    }}
    supertool._CONFIG_CHECKED = True


def _edit(tmp_path: Path, capsys) -> "tuple[int, str, str]":
    f = tmp_path / "s.json"
    f.write_text(BEFORE, encoding="utf-8")
    rc = supertool.main([f'edit:::"a":::"b":::{f}'])
    return rc, capsys.readouterr().out, f.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _stable_branch(monkeypatch):
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("f", ""))


# ---------------------------------------------------------------------------
# THE bug — the post-condition is the file's bytes
# ---------------------------------------------------------------------------

def test_a_stall_row_beside_an_unchanged_finding_does_not_revert_the_edit(
        tmp_path: Path, capsys) -> None:
    """One finding before, the same finding plus a stall row after.

    Nothing new was measured about the file. The `adapter` row is the schema's
    channel for an absence, and counting it took the delta to +1 on a
    `rollback_on_fail` validator, which wrote the file back to its pre-edit
    bytes. The edit was correct and it is gone.
    """
    _configure(_two_pass_adapter(tmp_path, REAL, MIXED))
    _rc, out, text = _edit(tmp_path, capsys)
    assert text == AFTER, (
        "a correct edit was reverted because a checker could not finish one of "
        f"its rows:{chr(10)}{out}")
    assert "rolled back" not in out, out


def test_a_stall_row_in_the_baseline_does_not_excuse_a_real_regression(
        tmp_path: Path, capsys) -> None:
    """The same defect pointing the other way, and it is the quieter half.

    Baseline: one finding plus a stall row (`count: 2`). After: two real
    findings (`count: 2`). The counts cancel, so a real new finding introduced
    by this edit read as unchanged, survived a `rollback_on_fail` validator and
    exited 0.
    """
    _configure(_two_pass_adapter(tmp_path, MIXED, TWO_REAL))
    _rc, out, text = _edit(tmp_path, capsys)
    assert text == BEFORE, (
        "a real new finding was excused because the baseline carried a row "
        f"nothing measured:{chr(10)}{out}")
    assert "rolled back" in out, out


# ---------------------------------------------------------------------------
# Direction 2 — the gate still gates, and the renderer still reads as it acts
# ---------------------------------------------------------------------------

def test_a_new_finding_beside_a_stall_row_still_rolls_the_edit_back(
        tmp_path: Path, capsys) -> None:
    """A gate that never fires is the louder bug. The finding is still a finding."""
    _configure(_two_pass_adapter(tmp_path, CLEAN, MIXED))
    _rc, out, text = _edit(tmp_path, capsys)
    assert text == BEFORE, out
    assert "rolled back" in out, out


def test_the_row_is_still_rendered_as_a_measurement_not_as_NOT_CHECKED(
        tmp_path: Path, capsys) -> None:
    """The rendering half must not change (#967).

    An adapter that reported a real finding measured the file, whatever else it
    also said. Printing `NOT CHECKED` over a real finding is the absence read as
    a pass, arriving inside the mechanism built to end it.
    """
    _configure(_two_pass_adapter(tmp_path, CLEAN, MIXED))
    _rc, out, _text = _edit(tmp_path, capsys)
    row = [ln for ln in out.splitlines() if ln.startswith("fake ")]
    assert row, out
    assert "NOT CHECKED" not in row[0], row[0]
    assert "unterminated object" in out, out


def test_the_delta_and_the_marker_do_not_disagree_about_the_same_row(
        tmp_path: Path, capsys) -> None:
    """The predicate says the red the caller sees and the revert it triggers can
    never disagree.

    The renderer draws its marker from that predicate and its arrow from the raw
    counts, so a stall row printed `1 -> 2 (+1)` beside a tick: one new error,
    and nothing wrong. Both numbers come from the same subtraction now.
    """
    _configure(_two_pass_adapter(tmp_path, REAL, MIXED))
    _rc, out, _text = _edit(tmp_path, capsys)
    row = [ln for ln in out.splitlines() if ln.startswith("fake ")]
    assert row, out
    assert "(+1)" not in row[0], (
        "the row claims this edit introduced an error, and the only new row is "
        f"the schema's channel for an absence: {row[0]}")


# ---------------------------------------------------------------------------
# The predicate itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("before,after,expected", [
    (CLEAN, MIXED, True),
    (REAL, MIXED, False),
    (MIXED, TWO_REAL, True),
    (MIXED, REAL, False),
    (MIXED, MIXED, False),
    (CLEAN, REAL, True),
    (REAL, REAL, False),
])
def test_regressed_counts_only_the_rows_that_measured_the_file(
        before: str, after: str, expected: bool) -> None:
    assert supertool._validator_regressed(
        json.loads(before), json.loads(after)) is expected


def test_an_all_adapter_payload_is_still_a_non_verdict() -> None:
    """The guard `_validator_not_checked` provides is untouched by the fix."""
    stalled = json.loads(_row(_stall()))
    assert supertool._validator_not_checked(stalled) is not None
    assert supertool._validator_regressed(json.loads(CLEAN), stalled) is False


def test_a_mixed_payload_is_still_a_verdict() -> None:
    """And so is its other half: the `all()` stays, per #1717's design note."""
    assert supertool._validator_not_checked(json.loads(MIXED)) is None
