"""`SUPERTOOL_REQUIRE_VALIDATORS` exits 0 on five ways the adapter itself fails (#975).

#967 fixed the case the adapter is healthy enough to report about *itself*:
`refusal.required()` runs inside the adapter, sees the binary is missing, and
emits an `adapter` error. `_validator_not_checked` keys on that self-report.

Every way the adapter is too broken to reach that code lands somewhere else.
`_validator_run_one` routes four of them into `_validator_unusable_reply`
(`skipped`) and one into the `TimeoutExpired` arm (an `orchestrator` error) —
and `_note_not_checked` sees neither, so the run exits 0. The row text is
honest in all five; only the exit code lies, which is the one thing
`supertool 'edit:...' && git commit` reads.

The core observed every one of these. It does not need the adapter's
cooperation to know the gate did not run.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool

REAL_FINDING = json.dumps({
    "tool": "fake", "ok": False, "count": 1,
    "errors": [{"line": 1, "col": 1, "severity": "error",
                "code": "SC2086", "msg": "unquoted expansion"}],
    "duration_ms": 1,
})
CLEAN = json.dumps({"tool": "fake", "ok": True, "count": 0, "errors": [],
                    "duration_ms": 1})
HONEST_SKIP = json.dumps({"tool": "fake", "skipped": "fake not found on PATH",
                          "duration_ms": 1})


def _adapter(script: Path, body: str) -> str:
    """A validator `cmd` spawnable under shell=False on every platform.

    `{python}` + `as_posix()`, for the reason
    `test_validators_required_not_checked_665.py::_py_adapter` gives.
    """
    script.write_text(body, encoding="utf-8")
    return f"{{python}} {script.as_posix()}"


#: The five. Each is the adapter body, and each is a *different* line of
#: `_validator_run_one` — that is the point: one predicate has to cover all of
#: them or the next one added slips through the same way.
BROKEN_ADAPTERS = {
    "prints_nothing": "",
    "prints_garbage": "import sys\nsys.stdout.write('not json at all\\n')\n",
    "crashes": "import sys\nsys.stderr.write('boom\\n')\nraise SystemExit(3)\n",
    "no_verdict_key": ("import sys, json\n"
                       "sys.stdout.write(json.dumps({'tool': 'fake'}))\n"),
    "times_out": "import time\ntime.sleep(30)\n",
}


def _set_one(tmp_path: Path, body: str, **extra) -> None:
    cmd = _adapter(tmp_path / "_adapter.py", body)
    spec = {"cmd": cmd, "match": "*", "cache": False,
            "hooks_into": ["edit", "replace", "replace_lines", "paste",
                           "append", "vim"]}
    spec.update(extra)
    supertool._CONFIG = {"validators": {"fake": spec}}
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
# THE bug — five adapters, five exit-0s, one gate that never ran
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", sorted(BROKEN_ADAPTERS))
def test_a_required_validator_that_broke_down_exits_nonzero(
        case: str, tmp_path: Path, capsys, monkeypatch) -> None:
    """`supertool 'edit:...' && git commit` must not proceed past an ungated edit.

    This is the whole failure mode the variable exists to prevent, and it is
    the assertion that was 0 for all five.
    """
    monkeypatch.setenv("SUPERTOOL_REQUIRE_VALIDATORS", "fake")
    _set_one(tmp_path, BROKEN_ADAPTERS[case], timeout=1)
    rc, out = _edit(tmp_path, capsys)
    assert rc == 1, out


@pytest.mark.parametrize("case", sorted(BROKEN_ADAPTERS))
def test_the_result_line_names_the_required_validator_that_broke_down(
        case: str, tmp_path: Path, capsys, monkeypatch) -> None:
    """The one line documented to survive `| tail -1` (#621).

    Asserted on that line, not on stdout: the *row* already said the file was
    not checked in all five cases while the footer read `1 op run, 1 write`.
    """
    monkeypatch.setenv("SUPERTOOL_REQUIRE_VALIDATORS", "fake")
    _set_one(tmp_path, BROKEN_ADAPTERS[case], timeout=1)
    _rc, out = _edit(tmp_path, capsys)
    line = _result_line(out)
    assert "1 validator NOT RUN (fake)" in line, line
    assert "NOT checked" in line, line


@pytest.mark.parametrize("case", sorted(BROKEN_ADAPTERS))
def test_the_row_says_not_checked_not_skipped(
        case: str, tmp_path: Path, capsys, monkeypatch) -> None:
    """Under an escalation, `skipped` is the wrong word.

    `skipped` is the honest third state for a checker nobody required. Once
    the operator has named it, a checker that declined is a configuration
    fault, and the row has to read the same way the exit code does.
    """
    monkeypatch.setenv("SUPERTOOL_REQUIRE_VALIDATORS", "fake")
    _set_one(tmp_path, BROKEN_ADAPTERS[case], timeout=1)
    _rc, out = _edit(tmp_path, capsys)
    assert "NOT CHECKED" in out, out


def test_the_star_form_escalates_too(tmp_path: Path, capsys, monkeypatch) -> None:
    """`*` is how CI names them, and is the form the audit reproduced with."""
    monkeypatch.setenv("SUPERTOOL_REQUIRE_VALIDATORS", "*")
    _set_one(tmp_path, BROKEN_ADAPTERS["prints_nothing"])
    rc, out = _edit(tmp_path, capsys)
    assert rc == 1, out
    assert "1 validator NOT RUN (fake)" in _result_line(out), out


def test_the_write_still_happens_and_is_not_rolled_back(
        tmp_path: Path, capsys, monkeypatch) -> None:
    """Disclosure, not refusal — #967's judgment call, and #969's.

    An unrunnable checker exits non-zero and says so. It does not revert an
    edit it formed no opinion about, even with `rollback_on_fail` set.
    """
    monkeypatch.setenv("SUPERTOOL_REQUIRE_VALIDATORS", "fake")
    _set_one(tmp_path, BROKEN_ADAPTERS["prints_nothing"], rollback_on_fail=True)
    rc, out = _edit(tmp_path, capsys)
    assert rc == 1, out
    assert (tmp_path / "s.sh").read_text(encoding="utf-8") == "exit0\n", out
    assert "1 write" in _result_line(out), out


# ---------------------------------------------------------------------------
# What must not regress — the escalation is opt-in and one-directional
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case",
                         sorted(set(BROKEN_ADAPTERS) - {"times_out"}))
def test_unset_leaves_an_adapter_breakdown_a_quiet_skip(
        case: str, tmp_path: Path, capsys, monkeypatch) -> None:
    """A laptop that never installed the tool has no information, and says so.

    Escalating this unconditionally is the trade #665 refused: every unrelated
    edit would fail because one optional checker is not installed.

    `times_out` was a fifth parameter here and is asserted separately below
    (#969). These four route through `_validator_unusable_reply` and become a
    `skipped` — the state #665 is about, and the one a missing optional tool
    actually produces. A timeout is not reachable that way, so the rationale in
    this docstring never covered it.
    """
    monkeypatch.delenv("SUPERTOOL_REQUIRE_VALIDATORS", raising=False)
    _set_one(tmp_path, BROKEN_ADAPTERS[case], timeout=1)
    rc, out = _edit(tmp_path, capsys)
    assert rc == 0, out
    assert "NOT RUN" not in out, out


def test_unset_still_escalates_a_core_timeout(
        tmp_path: Path, capsys, monkeypatch) -> None:
    """A timeout is a non-verdict whether or not anyone required the gate (#969).

    Reaching one needs the adapter present, spawned, and still running when the
    budget expired — so the checker this run cannot vouch for is a checker the
    operator configured and expects, not an optional tool nobody installed.
    Keeping it quiet protects none of #665's case and costs the whole of this
    one: the row read `NOT CHECKED` while the footer read `1 op run, 1 write`
    and the exit code read 0, which is the half `edit:... && git commit` reads.

    Symmetry decides the rest. An `adapter`-coded absence (#967) already
    escalates here with the variable unset. A core timeout is the same absence
    seen from the core's side of the pipe, and rendering the two identically
    while exiting differently is a distinction no consumer can act on.
    """
    monkeypatch.delenv("SUPERTOOL_REQUIRE_VALIDATORS", raising=False)
    _set_one(tmp_path, BROKEN_ADAPTERS["times_out"], timeout=1)
    rc, out = _edit(tmp_path, capsys)
    assert rc == 1, out
    assert "NOT CHECKED" in out, out
    assert "1 validator NOT RUN (fake)" in _result_line(out), out


def test_a_validator_not_named_is_not_escalated(
        tmp_path: Path, capsys, monkeypatch) -> None:
    """Naming gitleaks does not make an unrelated broken adapter fatal."""
    monkeypatch.setenv("SUPERTOOL_REQUIRE_VALIDATORS", "gitleaks,shellcheck")
    _set_one(tmp_path, BROKEN_ADAPTERS["prints_nothing"])
    rc, out = _edit(tmp_path, capsys)
    assert rc == 0, out
    assert "NOT RUN" not in out, out


def test_a_required_validator_that_answered_cleanly_is_still_a_pass(
        tmp_path: Path, capsys, monkeypatch) -> None:
    """The escalation only changes what happens when there is no verdict."""
    monkeypatch.setenv("SUPERTOOL_REQUIRE_VALIDATORS", "*")
    _set_one(tmp_path, f"import sys\nsys.stdout.write({CLEAN!r})\n")
    rc, out = _edit(tmp_path, capsys)
    assert rc == 0, out
    assert "NOT RUN" not in out, out


def test_a_required_validator_with_a_real_finding_still_reports_the_finding(
        tmp_path: Path, capsys, monkeypatch) -> None:
    """A finding is a verdict. It must not be relabelled `NOT CHECKED`.

    This is the defect pointing the other way: hiding a measured error behind
    the words that mean nothing was measured.
    """
    monkeypatch.setenv("SUPERTOOL_REQUIRE_VALIDATORS", "*")
    _set_one(tmp_path, f"import sys\nsys.stdout.write({REAL_FINDING!r})\n")
    _rc, out = _edit(tmp_path, capsys)
    assert "unquoted expansion" in out, out
    assert "NOT CHECKED" not in out, out


def test_an_adapter_declining_on_its_own_terms_is_left_alone(
        tmp_path: Path, capsys, monkeypatch) -> None:
    """An adapter that ran and chose to decline is not a core-observed breakdown.

    `warm_unsafe` (#345), an out-of-scope path (#263) and a resolver that maps
    a file to nothing all produce a plain `skipped` from a perfectly healthy
    adapter. Escalating those under `*` would fire on edits the operator never
    meant to gate, which is `SUPERTOOL_REQUIRE_VALIDATORS` becoming the noise
    it was built to replace. Adapters that *are* absent say so through
    `refusal.required()`, which #967 already routes.
    """
    monkeypatch.setenv("SUPERTOOL_REQUIRE_VALIDATORS", "*")
    _set_one(tmp_path, f"import sys\nsys.stdout.write({HONEST_SKIP!r})\n")
    rc, out = _edit(tmp_path, capsys)
    assert rc == 0, out
    assert "skipped" in out, out


# ---------------------------------------------------------------------------
# The core's reader of the variable, pinned to the adapters'
# ---------------------------------------------------------------------------

#: Every shape the variable is documented to take, plus the ones that broke
#: parsers before: whitespace, mixed case, the `os.pathsep` form, an empty
#: value, and a name that is a prefix of another.
_ENV_TABLE = [
    ("", "fake"), ("   ", "fake"),
    ("*", "fake"), ("fake", "fake"), ("FAKE", "fake"), ("fake", "FAKE"),
    (" fake ", "fake"), ("gitleaks,fake", "fake"), ("gitleaks, fake", "fake"),
    ("gitleaks", "fake"), ("fake2", "fake"), ("fak", "fake"),
    ("shellcheck,gitleaks", "gitleaks"),
]


@pytest.mark.parametrize("raw,tool", _ENV_TABLE)
def test_the_cores_reader_agrees_with_the_adapters(
        raw: str, tool: str, monkeypatch) -> None:
    """Two implementations of one rule, held to one answer.

    `_validator_required` cannot import `validators/common/refusal.py` — that
    package is loaded inside the adapter's own subprocess, and a gate that
    stops working when a sys.path resolution does is not a gate. The copy is
    the price; this table is what keeps it from drifting into a second rule
    (#895's lesson), and it is why `os.pathsep` and the case-folding are here
    rather than assumed.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_refusal_under_test",
        Path(supertool.__file__).parent / "validators" / "common" / "refusal.py")
    assert spec and spec.loader
    refusal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(refusal)

    monkeypatch.setenv("SUPERTOOL_REQUIRE_VALIDATORS", raw)
    assert supertool._validator_required(tool) is refusal.required(tool), raw


def test_the_pathsep_form_is_read_by_both(monkeypatch) -> None:
    """`PATH`-style separators, which differ between POSIX and Windows."""
    import os
    monkeypatch.setenv("SUPERTOOL_REQUIRE_VALIDATORS",
                       f"gitleaks{os.pathsep}fake")
    assert supertool._validator_required("fake") is True
    assert supertool._validator_required("nope") is False
