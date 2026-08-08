"""Under `SUPERTOOL_PARALLEL`, one op's footer counted another op's files (#1109).

`_VALIDATED_FILES` and `_NOT_CHECKED` are process-global lists. `_dispatch_impl`
took `len()` at op entry and sliced `[before:]` at op exit — an arithmetic that
is only per-op while exactly one op is appending. `validate` is in
`_PARALLEL_SAFE_OPS`, so six single-file `validate:` ops interleave their appends
against six independent snapshots, and every footer but one claims files its op
never opened.

`tests/conftest.py` already names this exact defect in the comment that adds
`_VALIDATED_FILES` to `RESET_GLOBALS` — *"a leak across calls in one process
would make one run's footer count another run's files"*. That closes it for
tests. This closes it for the tool.

The adapter here sleeps, deliberately. Without it the six ops can serialise by
luck and the miscount goes quiet — a test that passes because the race did not
happen is not evidence about the race.

Two invariants that a narrower fix would break, pinned below:

* the whole-call exit code still sees every validator that did not run. The
  per-op scope is what was wrong; `$SUPERTOOL_REQUIRE_VALIDATORS` is a statement
  about the *call*, and moving the accumulator per-op without an aggregate would
  turn a red into a silent green — the loud bug traded for the quiet one.
* a batch's footer still counts its own sub-ops. They append one frame deeper.
"""
from __future__ import annotations

import json
from pathlib import Path

import supertool

CLEAN = json.dumps({"tool": "fake", "ok": True, "count": 0, "errors": [],
                    "duration_ms": 1})
REAL_FINDING = json.dumps({
    "tool": "fake", "ok": False, "count": 1,
    "errors": [{"line": 1, "col": 1, "severity": "error",
                "code": "E999", "msg": "unterminated object"}],
    "duration_ms": 1,
})
SKIPPED = json.dumps({"tool": "fake", "skipped": "fake not installed",
                      "duration_ms": 1})
#: Not JSON at all — the core builds the no-verdict dict itself, which is the
#: only route that reaches `_NOT_CHECKED` under `$SUPERTOOL_REQUIRE_VALIDATORS`.
UNUSABLE = "this is not json"

WORKERS = 6


def _adapter(tmp_path: Path, reply_by_name: dict, delay: float = 0.15) -> str:
    """An adapter that answers by basename, slowly enough for the ops to overlap."""
    script = tmp_path / "_adapter.py"
    script.write_text(
        "import os, sys, time" + chr(10)
        + f"time.sleep({delay!r})" + chr(10)
        + f"replies = {reply_by_name!r}" + chr(10)
        + "name = os.path.basename(sys.argv[-1])" + chr(10)
        + f"sys.stdout.write(replies.get(name, {CLEAN!r}))" + chr(10),
        encoding="utf-8",
    )
    return f"{{python}} {script.as_posix()} {{file}}"


def _configure(cmd: str) -> None:
    supertool._CONFIG = {"validators": {
        "fake": {"cmd": cmd, "match": "*.json", "cache": False, "timeout": 30},
    }}
    supertool._CONFIG_CHECKED = True


def _result_lines(out: str) -> list:
    return [ln for ln in out.splitlines() if ln.startswith("[result]")]


def _files(tmp_path: Path, n: int = WORKERS) -> list:
    """`n` sibling .json files, bare names, cwd being `tmp_path`."""
    names = []
    for i in range(n):
        name = f"f{i}.json"
        (tmp_path / name).write_text('{"a": 1}' + chr(10), encoding="utf-8")
        names.append(name)
    return names


def _run_parallel(names: list, capsys, monkeypatch, tmp_path: Path) -> "tuple[int, str]":
    """One `validate:` op per file, dispatched in parallel. Not `validate:a,b,c`.

    The list form is one op and would carry a single honest footer; the defect
    needs several ops in flight at once, which is what a caller writing
    `supertool 'validate:x' 'validate:y'` under the env knob actually does.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUPERTOOL_PARALLEL", str(WORKERS))
    rc = supertool.main([f"validate:{n}" for n in names])
    return rc, capsys.readouterr().out


def test_each_parallel_op_footer_counts_only_its_own_file(
        tmp_path, capsys, monkeypatch) -> None:
    """Six single-file ops, six footers, each saying `1 file`."""
    _configure(_adapter(tmp_path, {}))
    rc, out = _run_parallel(_files(tmp_path), capsys, monkeypatch, tmp_path)
    lines = _result_lines(out)
    assert len(lines) == WORKERS, out
    assert all(
        ln == "[result] 1 file, 0 with findings, 0 not checked" for ln in lines
    ), out
    assert rc == 0, out


def test_a_finding_is_reported_by_the_one_op_that_found_it(
        tmp_path, capsys, monkeypatch) -> None:
    """`with findings` travels the same way — one finding, five false witnesses."""
    names = _files(tmp_path)
    _configure(_adapter(tmp_path, {names[3]: REAL_FINDING}))
    _rc, out = _run_parallel(names, capsys, monkeypatch, tmp_path)
    lines = _result_lines(out)
    assert len(lines) == WORKERS, out
    found = [ln for ln in lines if "1 with findings" in ln]
    assert len(found) == 1, out
    assert all("0 with findings" in ln for ln in lines if ln not in found), out


def test_a_skip_is_reported_by_the_one_op_that_skipped(
        tmp_path, capsys, monkeypatch) -> None:
    """`not checked` is the third element of the same tuple, and travelled too."""
    names = _files(tmp_path)
    _configure(_adapter(tmp_path, {names[1]: SKIPPED}))
    rc, out = _run_parallel(names, capsys, monkeypatch, tmp_path)
    lines = _result_lines(out)
    assert len(lines) == WORKERS, out
    assert len([ln for ln in lines if "1 not checked" in ln]) == 1, out
    assert rc == 0, "a skip discloses, it does not gate"


def test_the_not_run_names_do_not_travel_between_parallel_ops(
        tmp_path, capsys, monkeypatch) -> None:
    """`_NOT_CHECKED` is the sibling accumulator, sliced by the same arithmetic.

    One file's adapter is too broken to answer, so the core — not the adapter —
    concludes the gate did not run. Exactly one footer may name it.
    """
    monkeypatch.setenv("SUPERTOOL_REQUIRE_VALIDATORS", "*")
    names = _files(tmp_path)
    _configure(_adapter(tmp_path, {names[2]: UNUSABLE}))
    _rc, out = _run_parallel(names, capsys, monkeypatch, tmp_path)
    lines = _result_lines(out)
    assert len(lines) == WORKERS, out
    naming = [ln for ln in lines if "NOT RUN" in ln]
    assert len(naming) == 1, out
    assert "NOT RUN (fake)" in naming[0], naming[0]


def test_the_call_exit_code_still_sees_a_gate_that_did_not_run(
        tmp_path, capsys, monkeypatch) -> None:
    """The per-op scope was wrong; the per-CALL scope was right — keep it.

    `$SUPERTOOL_REQUIRE_VALIDATORS` is the operator saying these checkers must be
    present *here*, and `main` reads the whole call to decide. A fix that gave
    each op its own list and stopped there would leave nothing for `main` to
    read, and this red would go green — the exact absence-read-as-presence
    failure the knob exists to prevent.
    """
    monkeypatch.setenv("SUPERTOOL_REQUIRE_VALIDATORS", "*")
    names = _files(tmp_path)
    _configure(_adapter(tmp_path, {names[4]: UNUSABLE}))
    rc, out = _run_parallel(names, capsys, monkeypatch, tmp_path)
    assert rc == 1, out


def test_a_sequential_run_is_unchanged(tmp_path, capsys, monkeypatch) -> None:
    """The control from the issue: `SUPERTOOL_PARALLEL=0` was always correct."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUPERTOOL_PARALLEL", "0")
    names = _files(tmp_path, 3)
    _configure(_adapter(tmp_path, {}, delay=0.0))
    rc = supertool.main([f"validate:{n}" for n in names])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert _result_lines(out) == [
        "[result] 1 file, 0 with findings, 0 not checked"] * 3, out
