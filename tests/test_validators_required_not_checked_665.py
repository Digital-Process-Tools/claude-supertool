"""`SUPERTOOL_REQUIRE_VALIDATORS` escalated, and the receipt read as a pass (#665).

`refusal.required()` promises that in CI an absent validator stops being a
`skipped` and becomes a loud `adapter` error. The error is produced — every
adapter-level test in `test_validators_shellcheck_665.py` and its two siblings
passes — and then the *core* files it as a finding about the file, diffs it
against a baseline pass that produced the identical error, and prints

    shellcheck  : 1 err       (pre-existing — not from this edit)

with `[result] 1 op run, 1 write` and exit 0. Every layer that a reader or a
`&&` chain looks at says the edit was fine, about a file no checker opened.

The mechanism is the before/after delta. An adapter error is not a finding
about the file, so it must not be subtracted from another one: with the tool
absent for both snapshots the counts are equal, the row is labelled
`pre-existing`, and the one line documented to survive `| tail -1` never
mentions it.

So these tests are deliberately end-to-end through `supertool.main()` — the
exit code and the `[result]` line, not "some string appears in stdout". The
adapter suites already assert the message and they were all green while this
shipped.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool

REPO = Path(__file__).resolve().parent.parent
SHELLCHECK = REPO / "validators" / "shellcheck" / "shellcheck.py"

#: The exact payload `validators/*/[tool].py::_adapter_error` emits when
#: `refusal.required()` is true and the binary is absent.
ABSENT_MSG = ("fake is named in $SUPERTOOL_REQUIRE_VALIDATORS but could not "
              "run, so this file was NOT checked: fake not found on PATH — "
              "`brew install fake`")
ADAPTER_ERROR = json.dumps({
    "tool": "fake", "ok": False, "count": 1,
    "errors": [{"line": None, "col": None, "severity": "error",
                "code": "adapter", "msg": ABSENT_MSG}],
    "duration_ms": 1,
})
REAL_FINDING = json.dumps({
    "tool": "fake", "ok": False, "count": 1,
    "errors": [{"line": 1, "col": 1, "severity": "error",
                "code": "SC2086", "msg": "unquoted expansion"}],
    "duration_ms": 1,
})


def _py_adapter(script: Path, payload: str) -> str:
    """A validator `cmd` spawnable under shell=False on every platform.

    Same reasoning as `tests/test_result_footer_621.py::_py_adapter`: not
    `echo`, not a shebang, `{python}` + `as_posix()`.
    """
    script.write_text(f"import sys\nsys.stdout.write({payload!r})\n",
                      encoding="utf-8")
    return f"{{python}} {script.as_posix()}"


def _set_one(tmp_path: Path, payload: str) -> None:
    cmd = _py_adapter(tmp_path / "_adapter.py", payload)
    supertool._CONFIG = {"validators": {
        "fake": {"cmd": cmd, "match": "*", "cache": False,
                 "hooks_into": ["edit", "replace", "replace_lines", "paste",
                                "append", "vim"]},
    }}
    supertool._CONFIG_CHECKED = True


def _result_line(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("[result] "):
            return line
    raise AssertionError(f"no [result] line in:\n{out}")


@pytest.fixture(autouse=True)
def _stable_branch(monkeypatch):
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("f", ""))


def _edit(tmp_path: Path, capsys) -> "tuple[int, str]":
    f = tmp_path / "s.sh"
    f.write_text("exit 0\n", encoding="utf-8")
    rc = supertool.main([f"edit:::exit :::exit:::{f}"])
    return rc, capsys.readouterr().out


# ---------------------------------------------------------------------------
# THE bug — the escalation itself reads as a pass
# ---------------------------------------------------------------------------

def test_a_validator_that_could_not_run_is_not_labelled_pre_existing(
        tmp_path: Path, capsys) -> None:
    """The adapter error is identical before and after, so the delta cancels.

    `(pre-existing — not from this edit)` is a claim about the *file*: it says
    a real finding was already there. Nothing was found and nothing was
    looked at.
    """
    _set_one(tmp_path, ADAPTER_ERROR)
    _rc, out = _edit(tmp_path, capsys)
    assert "pre-existing" not in out, out
    assert "NOT CHECKED" in out, out


def test_the_result_line_names_the_validator_that_did_not_run(
        tmp_path: Path, capsys) -> None:
    """The one line documented to survive `| tail -1` (#621).

    Asserted on that line specifically, not on stdout: the row above it has
    carried the words `NOT checked` since the escalation shipped, and the
    reader who pipes still saw `1 op run, 1 write`.
    """
    _set_one(tmp_path, ADAPTER_ERROR)
    _rc, out = _edit(tmp_path, capsys)
    line = _result_line(out)
    assert "1 validator NOT RUN (fake)" in line, line
    assert "NOT checked" in line, line


def test_a_validator_that_could_not_run_exits_nonzero(
        tmp_path: Path, capsys) -> None:
    """`supertool ... && git commit` must not proceed past an ungated edit."""
    _set_one(tmp_path, ADAPTER_ERROR)
    rc, _out = _edit(tmp_path, capsys)
    assert rc == 1


def test_the_write_still_happens(tmp_path: Path, capsys) -> None:
    """Disclosure, not refusal.

    The judgment call: an unrunnable checker is reported and exits nonzero, it
    does not revert the edit or block the tool. Trading the loud bug for a
    tool that refuses to work is the other half of the same mistake.
    """
    _set_one(tmp_path, ADAPTER_ERROR)
    _rc, out = _edit(tmp_path, capsys)
    assert (tmp_path / "s.sh").read_text(encoding="utf-8") == "exit0\n"
    assert "1 write" in _result_line(out)


# ---------------------------------------------------------------------------
# End to end, with the real adapter and no shellcheck on PATH
# ---------------------------------------------------------------------------

def test_end_to_end_with_the_real_shellcheck_adapter(
        tmp_path: Path, capsys, monkeypatch) -> None:
    """The auditor's reproduction, as a test.

    `PATH` is emptied for the spawned adapter, so `shutil.which` genuinely
    fails; the adapter is reached by absolute interpreter path, so the
    escalation runs for real rather than through a stub.
    """
    monkeypatch.setenv("PATH", str(tmp_path / "no-such-bin"))
    monkeypatch.setenv("SUPERTOOL_REQUIRE_VALIDATORS", "*")
    supertool._CONFIG = {"validators": {
        "shellcheck": {"cmd": f"{{python}} {SHELLCHECK.as_posix()} {{file}}",
                       "match": "*.sh", "cache": False,
                       "hooks_into": ["edit"]},
    }}
    supertool._CONFIG_CHECKED = True
    rc, out = _edit(tmp_path, capsys)
    assert rc == 1, out
    assert "pre-existing" not in out, out
    assert "1 validator NOT RUN (shellcheck)" in _result_line(out), out


# ---------------------------------------------------------------------------
# What must not regress
# ---------------------------------------------------------------------------

def test_the_unescalated_skip_is_untouched(
        tmp_path: Path, capsys, monkeypatch) -> None:
    """Without the variable set, an absent tool is still `skipped` and exit 0.

    This is the behaviour that was already right. A fix that made every
    missing optional binary fatal would be the loud bug traded for a tool
    nobody can run.
    """
    monkeypatch.setenv("PATH", str(tmp_path / "no-such-bin"))
    monkeypatch.delenv("SUPERTOOL_REQUIRE_VALIDATORS", raising=False)
    supertool._CONFIG = {"validators": {
        "shellcheck": {"cmd": f"{{python}} {SHELLCHECK.as_posix()} {{file}}",
                       "match": "*.sh", "cache": False,
                       "hooks_into": ["edit"]},
    }}
    supertool._CONFIG_CHECKED = True
    rc, out = _edit(tmp_path, capsys)
    assert rc == 0, out
    assert "skipped" in out, out
    assert "NOT RUN" not in out, out


def test_a_real_pre_existing_finding_still_reads_as_pre_existing(
        tmp_path: Path, capsys) -> None:
    """The delta framing is correct for findings and stays."""
    _set_one(tmp_path, REAL_FINDING)
    rc, out = _edit(tmp_path, capsys)
    assert "(pre-existing — not from this edit)" in out, out
    assert "NOT RUN" not in _result_line(out)
    assert rc == 0, out


def test_a_clean_validator_says_nothing_new(tmp_path: Path, capsys) -> None:
    _set_one(tmp_path, json.dumps({"tool": "fake", "ok": True, "count": 0,
                                   "errors": [], "duration_ms": 1}))
    rc, out = _edit(tmp_path, capsys)
    assert rc == 0, out
    assert "NOT RUN" not in out, out


# ---------------------------------------------------------------------------
# F6 — a truncation with no marker, inside the disclosure text
# ---------------------------------------------------------------------------

def test_a_truncated_cell_says_it_was_truncated() -> None:
    """`apt install shellche)` and ``(`brew instal)`` are what shipped.

    A reason string cut mid-word with no marker is indistinguishable from a
    reason string that ended there — inside the one field whose entire job is
    to disclose why nothing was checked.
    """
    long = "x" * 200
    cell = supertool._flat_cell(long, 80)
    assert len(cell) <= 80, cell
    assert cell.endswith("…"), cell
    assert supertool._flat_cell("short", 80) == "short"


def test_the_skipped_reason_row_carries_the_marker(
        tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "no-such-bin"))
    monkeypatch.delenv("SUPERTOOL_REQUIRE_VALIDATORS", raising=False)
    supertool._CONFIG = {"validators": {
        "shellcheck": {"cmd": f"{{python}} {SHELLCHECK.as_posix()} {{file}}",
                       "match": "*.sh", "cache": False,
                       "hooks_into": ["edit"]},
    }}
    supertool._CONFIG_CHECKED = True
    _rc, out = _edit(tmp_path, capsys)
    row = next(ln for ln in out.splitlines() if ln.startswith("shellcheck"))
    assert "skipped" in row, row
    # The hint is longer than the 80-column cell, so it is cut — and says so.
    assert "…" in row, row
