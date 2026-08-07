"""A `rollback_on_fail` validator that could not run reverted a good edit (#969).

`_validator_regressed` compares an error count before an edit with the count
after it. An `adapter` error is `ok: false, count: 1` by SCHEMA.md's own
design — so a validator whose tool becomes unavailable *between* the two
snapshots goes 0 → 1, reads as a regression, and on a validator configured
`rollback_on_fail: true` the edit is written back to its pre-edit bytes.

The edit was fine. Nothing was ever checked. The work is gone — and this is the
only failure mode on the tracker that *destroys* rather than misinforms.

#967 already settled the principle one commit upstream: a result whose errors
are all `code == "adapter"` is a non-verdict, not a finding. It gave that
treatment to the renderer, the `[result]` line and the exit code, and not to
the one path that can delete an edit. `_validator_regressed`'s own docstring
claims "the red the caller sees and the revert it triggers can never disagree";
after #967 that was false — the row said `NOT CHECKED` while the file was
reverted underneath it.

Two things these tests pin that a narrower fix would miss:

* **The core's own timeout is the same absence.** `code: "orchestrator"`,
  `ok: false`, `count: 1`, from `_validator_run_one`'s `TimeoutExpired` arm. It
  needs no exotic config — a loaded machine and jsonlint's 10s budget will do —
  and it reverted edits by exactly the arithmetic above.
* **The `before` side is the same defect pointing the other way.** A
  non-verdict *baseline* has `count: 1` too, so a real new finding after it
  cancels to a delta of zero: the edit that broke the file was labelled
  `(pre-existing — not from this edit)`, was not rolled back, and exited 0.

Deliberately end-to-end through `supertool.main()` and asserted on the file's
bytes: the post-condition is that the work survives, not that some branch ran.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool

CLEAN = json.dumps({"tool": "fake", "ok": True, "count": 0, "errors": [],
                    "duration_ms": 1})
ADAPTER_ERROR = json.dumps({
    "tool": "fake", "ok": False, "count": 1,
    "errors": [{"line": None, "col": None, "severity": "error",
                "code": "adapter",
                "msg": "fake exited 2 and said nothing about the file"}],
    "duration_ms": 1,
})
REAL_FINDING = json.dumps({
    "tool": "fake", "ok": False, "count": 1,
    "errors": [{"line": 1, "col": 1, "severity": "error",
                "code": "E999", "msg": "unterminated object"}],
    "duration_ms": 1,
})

BEFORE = '{"a": 1}\n'
AFTER = '{"b": 1}\n'


def _two_pass_adapter(tmp_path: Path, first: str, second: str,
                      sleep_second: float = 0.0) -> str:
    """An adapter that answers `first` on call 1 and `second` on every call after.

    The baseline pass and the post-edit pass are two separate spawns of the
    same command, which is exactly why the defect exists: a tool can be there
    for one and gone for the other. A counter file is the only state that
    survives between them.

    `{python}` + `as_posix()` rather than a shebang, so it spawns under
    `shell=False` on every platform (same reasoning as `test_result_footer_621`).
    """
    state = tmp_path / "_calls.txt"
    script = tmp_path / "_adapter.py"
    script.write_text(
        "import pathlib, sys, time" + chr(10)
        + f"state = pathlib.Path({str(state)!r})" + chr(10)
        + "n = int(state.read_text()) if state.exists() else 0" + chr(10)
        + "state.write_text(str(n + 1))" + chr(10)
        + f"if n and {sleep_second!r}:" + chr(10)
        + f"    time.sleep({sleep_second!r})" + chr(10)
        + f"sys.stdout.write({first!r} if n == 0 else {second!r})" + chr(10),
        encoding="utf-8",
    )
    return f"{{python}} {script.as_posix()}"


def _configure(cmd: str, timeout: int = 10) -> None:
    supertool._CONFIG = {"validators": {
        "fake": {"cmd": cmd, "match": "*.json", "cache": False,
                 "rollback_on_fail": True,
                 "hooks_into": ["edit", "replace", "replace_lines", "paste",
                                "append", "vim"],
                 "timeout": timeout},
    }}
    supertool._CONFIG_CHECKED = True


def _result_line(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("[result] "):
            return line
    raise AssertionError(f"no [result] line in:{chr(10)}{out}")


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

def test_an_adapter_error_after_the_edit_does_not_revert_it(
        tmp_path: Path, capsys) -> None:
    """Clean baseline, unrunnable tool afterwards. The edit is not the suspect."""
    _configure(_two_pass_adapter(tmp_path, CLEAN, ADAPTER_ERROR))
    _rc, out, text = _edit(tmp_path, capsys)
    assert text == AFTER, (
        f"the edit was reverted by a checker that formed no opinion:{chr(10)}{out}")
    assert "rolled back" not in out, out


def test_the_surviving_edit_is_disclosed_and_exits_nonzero(
        tmp_path: Path, capsys) -> None:
    """Keeping the edit is only defensible if the reader is told the gate did not run.

    Asserted on the `[result]` line specifically — the one line the docs tell
    readers to trust under `| tail -1`.
    """
    _configure(_two_pass_adapter(tmp_path, CLEAN, ADAPTER_ERROR))
    rc, out, _text = _edit(tmp_path, capsys)
    line = _result_line(out)
    assert "1 write" in line, line
    assert "1 validator NOT RUN (fake)" in line, line
    assert rc == 1, out


def test_a_core_timeout_after_the_edit_does_not_revert_it(
        tmp_path: Path, capsys) -> None:
    """`code: "orchestrator"` is the core's own absence, and it reverted edits too.

    Not a variant worth splitting off: it reaches the same two-line comparison
    with the same fabricated `count: 1`, and unlike a vanished binary it needs
    nothing but a slow machine.
    """
    _configure(_two_pass_adapter(tmp_path, CLEAN, CLEAN, sleep_second=3.0),
               timeout=1)
    rc, out, text = _edit(tmp_path, capsys)
    assert text == AFTER, (
        f"a timed-out checker reverted an edit it never looked at:{chr(10)}{out}")
    assert "1 write" in _result_line(out), out
    assert rc == 1, out


def test_a_non_verdict_baseline_is_not_a_count_to_subtract(
        tmp_path: Path, capsys) -> None:
    """The same defect pointing the other way, on the same two lines.

    Baseline could not run (`count: 1`), the edit introduces a real finding
    (`count: 1`) — the delta is zero, so the file that this edit broke was
    labelled `pre-existing`, kept, and reported at exit 0.
    """
    _configure(_two_pass_adapter(tmp_path, ADAPTER_ERROR, REAL_FINDING))
    _rc, out, text = _edit(tmp_path, capsys)
    assert "pre-existing" not in out, (
        f"nothing measured the file before the edit:{chr(10)}{out}")
    assert text == BEFORE, (
        f"a real new finding on a rollback validator must revert:{chr(10)}{out}")


# ---------------------------------------------------------------------------
# Direction 2 — the gate still gates. A gate that never fires is the louder bug.
# ---------------------------------------------------------------------------

def test_a_real_new_finding_still_rolls_the_edit_back(
        tmp_path: Path, capsys) -> None:
    _configure(_two_pass_adapter(tmp_path, CLEAN, REAL_FINDING))
    _rc, out, text = _edit(tmp_path, capsys)
    assert text == BEFORE, out
    assert "rolled back" in out, out
    assert "0 writes" in _result_line(out), out


def test_a_pre_existing_finding_measured_both_times_still_reads_pre_existing(
        tmp_path: Path, capsys) -> None:
    """Two real verdicts do subtract. #969 is about non-verdicts only."""
    _configure(_two_pass_adapter(tmp_path, REAL_FINDING, REAL_FINDING))
    rc, out, text = _edit(tmp_path, capsys)
    assert "(pre-existing — not from this edit)" in out, out
    assert text == AFTER, out
    assert rc == 0, out


# ---------------------------------------------------------------------------
# The predicate itself
# ---------------------------------------------------------------------------

def _adapter_result() -> dict:
    return json.loads(ADAPTER_ERROR)


def test_regressed_is_false_for_an_adapter_error_against_a_clean_baseline() -> None:
    clean = {"tool": "fake", "ok": True, "count": 0}
    assert supertool._validator_regressed(clean, _adapter_result()) is False


def test_regressed_is_false_for_a_timeout_against_a_clean_baseline() -> None:
    clean = {"tool": "fake", "ok": True, "count": 0}
    timed_out = {"tool": "fake", "ok": False, "count": 1, "timeout": True,
                 "errors": [{"line": None, "col": None, "severity": "error",
                             "code": "orchestrator", "msg": "timeout after 10s"}]}
    assert supertool._validator_regressed(clean, timed_out) is False


def test_regressed_is_true_for_a_real_finding_after_a_non_verdict_baseline() -> None:
    """No baseline is not a clean baseline, but it is not a count either."""
    assert supertool._validator_regressed(
        _adapter_result(), json.loads(REAL_FINDING)) is True


def test_an_adapter_error_mixed_with_real_findings_still_regresses() -> None:
    """One adapter row among findings means the tool DID measure the file."""
    mixed = {"tool": "fake", "ok": False, "count": 2, "errors": [
        {"line": 1, "col": 1, "severity": "error", "code": "E999", "msg": "x"},
        {"line": None, "col": None, "severity": "error", "code": "adapter",
         "msg": "partial"},
    ]}
    clean = {"tool": "fake", "ok": True, "count": 0}
    assert supertool._validator_regressed(clean, mixed) is True

def test_a_timeout_reads_the_same_whether_or_not_it_was_required(
        tmp_path: Path, capsys, monkeypatch) -> None:
    """One failure, one spelling.

    A required gate renders through `_validator_gate_did_not_run` (#975) and an
    unrequired one through `_validator_no_verdict`. Both are the core saying it
    got no answer; if the two rows disagree about *what happened*, the reader
    has to know which branch produced theirs before they can read it, and the
    only thing the variable actually changes is the exit code.

    Compared on the status and reason columns, not the whole row: the elapsed
    time legitimately differs between two runs.
    """
    whys = []
    for required in (False, True):
        if required:
            monkeypatch.setenv("SUPERTOOL_REQUIRE_VALIDATORS", "fake")
        else:
            monkeypatch.delenv("SUPERTOOL_REQUIRE_VALIDATORS", raising=False)
        d = tmp_path / ("req" if required else "unreq")
        d.mkdir()
        _configure(_two_pass_adapter(d, CLEAN, CLEAN, sleep_second=3.0),
                   timeout=1)
        _rc, out, _text = _edit(d, capsys)
        row = [ln for ln in out.splitlines() if ln.startswith("fake ")]
        assert row, out
        assert "NOT CHECKED" in row[0], row[0]
        whys.append(row[0].split("NOT CHECKED", 1)[1].rsplit("  ", 1)[0].strip())
    assert whys[0] == "(timed out — no verdict about this file)", whys
    assert whys[0] == whys[1], whys


# ---------------------------------------------------------------------------
# The `validate:` op had no [result] line at all
# ---------------------------------------------------------------------------

def _validate(tmp_path: Path, capsys) -> "tuple[int, str]":
    f = tmp_path / "s.json"
    f.write_text(BEFORE, encoding="utf-8")
    rc = supertool.main([f"validate:{f}"])
    return rc, capsys.readouterr().out


def test_validate_footer_names_the_validator_that_did_not_run(
        tmp_path: Path, capsys) -> None:
    """`validate:` mutates nothing, so the footer was gated out entirely.

    Its non-verdict was in the row and in the exit code and nowhere in the
    summary line — which is the line the docs tell readers to trust.
    """
    _configure(_two_pass_adapter(tmp_path, ADAPTER_ERROR, ADAPTER_ERROR))
    rc, out = _validate(tmp_path, capsys)
    line = _result_line(out)
    assert "1 validator NOT RUN (fake)" in line, line
    assert "NOT checked" in line, line
    assert "\\n" not in out, (
        "the footer ends in a literal backslash-n rather than a newline, so it "
        "does not terminate — and `| tail -1`, which this branch exists to feed, "
        f"returns it joined to whatever prints next:{chr(10)}{out}")
    assert rc == 1, out


def test_validate_footer_does_not_count_ops_it_never_ran(
        tmp_path: Path, capsys) -> None:
    """`0 ops run, 0 writes` about a read-only op is a number that means nothing."""
    _configure(_two_pass_adapter(tmp_path, ADAPTER_ERROR, ADAPTER_ERROR))
    _rc, out = _validate(tmp_path, capsys)
    line = _result_line(out)
    assert "ops run" not in line, line
    assert "writes" not in line, line


def test_a_clean_validate_gains_no_footer(tmp_path: Path, capsys) -> None:
    """The word appears when a checker declined and never otherwise (#621)."""
    _configure(_two_pass_adapter(tmp_path, CLEAN, CLEAN))
    rc, out = _validate(tmp_path, capsys)
    assert "[result]" not in out, out
    assert rc == 0, out
