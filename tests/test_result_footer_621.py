"""#621 — the last lines of a call must not lie about whether it changed anything.

The reported failure: a mutating op prints its result summary *above* the
`[validators]` block, so `| tail -4` ends on `git-status : ok` and reads as
"that worked" whether or not a single byte moved. A real incident followed —
an 11-file `replace` reported to a teammate as having matched nothing, and
later a no-matched `batch` edit reported as landed, which cost a 14-leg CI run.

The invariant these tests pin is stronger and more testable than "print the
summary last":

    an op which changed nothing must not end with output that looks like
    an op which did.

So they assert on the *rendered tail* — what a reader piping to `tail` sees —
not on any internal return value.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import supertool


OK_PAYLOAD = json.dumps({"tool": "fake", "ok": True, "count": 0,
                         "errors": [], "duration_ms": 1})


def _set_validators(cfg: dict) -> None:
    supertool._CONFIG = {"validators": cfg}
    supertool._CONFIG_CHECKED = True


def _py_adapter(script: Path, body: str) -> str:
    """A validator `cmd` that actually runs under shell=False on every platform.

    Deliberately not `echo '<json>'` and not a `#!/bin/sh` script. Windows has
    no `/bin/sh` and no shebang handling, `echo` is a cmd.exe builtin rather
    than an executable, and a raw `str(Path)` is eaten by POSIX-mode
    `shlex.split` (#637). All three fail the same way: the adapter cannot be
    spawned, so the validator correctly reports `skipped`, no rollback fires,
    and a test whose premise was "this validator fails" silently tests nothing.
    `{python}` + `as_posix()` is the pattern `_counter_cmd` in
    tests/test_validators.py already uses, for exactly this reason.
    """
    script.write_text(body, encoding="utf-8")
    return f"{{python}} {script.as_posix()}"


def _noisy_validator(tmp_path: Path) -> None:
    """A validator that always passes, so every mutating op ends in a green
    `[validators]` block — the exact condition that hides the summary."""
    cmd = _py_adapter(tmp_path / "_ok_adapter.py",
                      f"import sys\nsys.stdout.write({OK_PAYLOAD!r})\n")
    _set_validators({
        "fake": {"cmd": cmd, "hooks_into": ["edit", "replace", "paste",
                                            "append", "replace_lines", "vim"],
                 "match": "*", "cache": False},
    })


def _tail(out: str, n: int = 4) -> str:
    return "\n".join(out.rstrip().splitlines()[-n:])


def _stable_tail(out: str, n: int = 4) -> str:
    """`_tail` with validator durations blanked.

    Without this the invariant test can pass for the wrong reason: two
    identical tails differing only by `0.1s` vs `0.2s` compare unequal, and the
    test would report the bug fixed while nothing had changed. Seen under xdist.
    """
    return re.sub(r"\d+\.\d+s", "Ns", _tail(out, n))


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------

def test_zero_match_tail_is_distinguishable_from_applied_tail(tmp_path: Path, monkeypatch) -> None:
    """THE bug. Two calls, one that wrote and one that did not, read the same
    through `tail -4`."""
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    _noisy_validator(tmp_path)
    f = tmp_path / "x.txt"
    f.write_text("alpha\n")

    missed = supertool.dispatch(f"replace:::NOPE_NOT_THERE:::x:::{f}")
    applied = supertool.dispatch(f"replace:::alpha:::gamma:::{f}")

    assert f.read_text(encoding="utf-8") == "gamma\n", "the second call must really have written"
    assert "[validators]" in missed and "[validators]" in applied, "precondition: long tail"
    assert _stable_tail(missed) != _stable_tail(applied)


def test_zero_match_tail_says_nothing_changed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    _noisy_validator(tmp_path)
    f = tmp_path / "x.txt"
    f.write_text("alpha\n")
    out = supertool.dispatch(f"replace:::NOPE_NOT_THERE:::x:::{f}")
    assert ("[result] 1 op run, 0 writes, 1 skipped — nothing changed on disk"
            in _tail(out))


def test_applied_tail_reports_the_write(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    _noisy_validator(tmp_path)
    f = tmp_path / "x.txt"
    f.write_text("alpha\n")
    out = supertool.dispatch(f"replace:::alpha:::gamma:::{f}")
    assert "[result] 1 op run, 1 write" in _tail(out)
    assert "nothing changed" not in out


def test_failed_edit_tail_says_nothing_changed(tmp_path: Path, monkeypatch) -> None:
    """An `ERROR:` receipt is equally above the validators block."""
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    _noisy_validator(tmp_path)
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    out = supertool.dispatch(f"edit:::nope = 1:::a = 2:::{f}")
    assert ("[result] 1 op run, 0 writes, 1 skipped — nothing changed on disk"
            in _tail(out))


# ---------------------------------------------------------------------------
# Placement — the branch line stays last, #381's tests depend on it
# ---------------------------------------------------------------------------

def test_result_sits_directly_above_the_branch_line(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    _noisy_validator(tmp_path)
    f = tmp_path / "x.txt"
    f.write_text("alpha\n")
    out = supertool.dispatch(f"replace:::alpha:::gamma:::{f}")
    lines = out.rstrip().splitlines()
    assert lines[-1] == "[branch: my-feature]"
    assert lines[-2].startswith("[result] ")


def test_result_footer_survives_absence_of_a_branch(tmp_path: Path, monkeypatch) -> None:
    """Outside a repo there is no branch line, and the output would otherwise
    end on `[validators]` — the precise shape #621 is about."""
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("", ""))
    _noisy_validator(tmp_path)
    f = tmp_path / "x.txt"
    f.write_text("alpha\n")
    out = supertool.dispatch(f"replace:::NOPE:::x:::{f}")
    assert "[branch:" not in out
    assert out.rstrip().splitlines()[-1].startswith("[result] ")


def test_summary_stays_where_it_was(tmp_path: Path, monkeypatch) -> None:
    """The footer is additive. Moving the summary below `[validators]` would
    break every positional reader and every existing ordering assertion, so
    the detailed receipt must still precede the validators block."""
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    _noisy_validator(tmp_path)
    f = tmp_path / "x.txt"
    f.write_text("alpha\n")
    out = supertool.dispatch(f"replace:::alpha:::gamma:::{f}")
    assert out.index("Done: 'alpha'") < out.index("[validators]")


# ---------------------------------------------------------------------------
# Batches — requested vs applied
# ---------------------------------------------------------------------------

def test_batch_footer_distinguishes_ops_run_from_writes(tmp_path: Path, monkeypatch) -> None:
    """The #634 incident: one no-match in the middle of five successes. The
    footer alone has to expose it."""
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    _noisy_validator(tmp_path)
    f = tmp_path / "x.txt"
    f.write_text("alpha\nbeta\n")
    payload = tmp_path / "ops.json"
    payload.write_text(json.dumps([
        {"op": "replace", "path": str(f), "old": "alpha", "new": "ALPHA"},
        {"op": "replace", "path": str(f), "old": "NOPE_NOT_THERE", "new": "x"},
        {"op": "replace", "path": str(f), "old": "beta", "new": "BETA"},
    ]))
    out = supertool.dispatch(f"batch:@{payload}")
    assert "[result] 3 ops run, 2 writes" in _tail(out)
    # Two since #1027 -- the leading copy and the footer. What this guards is
    # unchanged: never one count per SUB-OP, which a total alone would miss, so
    # the region between the first op and the footer is checked directly.
    assert out.count("[result] ") == 2, "one leading count and one footer"
    between = out.split("--- replace:", 1)[-1].rsplit("[result] ", 1)[0]
    assert "[result] " not in between, "no count inside the per-op results"


def test_batch_where_every_op_misses_says_nothing_changed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    _noisy_validator(tmp_path)
    f = tmp_path / "x.txt"
    f.write_text("alpha\n")
    payload = tmp_path / "ops.json"
    payload.write_text(json.dumps([
        {"op": "replace", "path": str(f), "old": "NOPE1", "new": "x"},
        {"op": "replace", "path": str(f), "old": "NOPE2", "new": "y"},
    ]))
    out = supertool.dispatch(f"batch:@{payload}")
    assert f.read_text(encoding="utf-8") == "alpha\n"
    assert ("[result] 2 ops run, 0 writes, 2 skipped — nothing changed on disk"
            in _tail(out))


def test_nested_batch_reports_one_footer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    f = tmp_path / "x.txt"
    f.write_text("a = 1\n")
    inner = tmp_path / "inner.json"
    inner.write_text(json.dumps([{"op": "edit", "path": str(f), "old": "a = 1", "new": "a = 2"}]))
    outer = tmp_path / "outer.json"
    outer.write_text(json.dumps([{"op": "batch", "path": f"@{inner}"}]))
    out = supertool.dispatch(f"batch:@{outer}")
    # The nested batch is the point: the INNER one prints neither a leading
    # count nor a footer, so the two here both belong to the outer call (#392,
    # and #1027 for the leading one). Three would mean the inner batch had
    # started reporting for itself again.
    assert out.count("[result] ") == 2
    assert "[result] 1 op run, 1 write" in _tail(out)
    assert out.startswith("--- batch:")
    assert out.splitlines()[1].startswith("[result] "), out


# ---------------------------------------------------------------------------
# A write that did not stick is not a write
# ---------------------------------------------------------------------------

def test_rolled_back_edit_reports_zero_writes(tmp_path: Path) -> None:
    """A validator rollback restores the file. The footer must not claim the
    edit landed — that would trade the loud failure for a quiet one."""
    f = tmp_path / "x.py"
    original = "a = 1\n"
    f.write_text(original)
    fail = json.dumps({"tool": "fake", "ok": False, "count": 1,
                       "errors": [{"line": 1, "msg": "boom", "code": "x",
                                   "severity": "error"}], "duration_ms": 1})
    counter = tmp_path / "n"
    counter.write_text("0")
    cmd = _py_adapter(tmp_path / "_counter_adapter.py",
                      "import pathlib, sys\n"
                      f"p = pathlib.Path({str(counter)!r})\n"
                      "n = int(p.read_text())\n"
                      "p.write_text(str(n + 1))\n"
                      f"sys.stdout.write({OK_PAYLOAD!r} if n == 0 else {fail!r})\n")
    _set_validators({
        "fake": {"cmd": cmd, "hooks_into": ["edit"], "match": "*.py",
                 "rollback_on_fail": True, "cache": False},
    })
    out = supertool.dispatch(f"edit:::a = 1:::a = 2:::{f}")
    assert "skipped" not in out, "the adapter must really run — a skip would test nothing"
    assert "rolled back" in out
    assert f.read_text(encoding="utf-8") == original
    assert ("[result] 1 op run, 0 writes, 1 rolled back — nothing changed on "
            "disk; 1 edit was reverted after validation and did NOT land"
            in _tail(out)), out


# ---------------------------------------------------------------------------
# Scope — read-only and preview ops stay out of it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("arg", ["read:{f}", "grep:alpha:{f}", "grep:NOPE:{f}",
                                 "wc:{f}", "stat:{f}", "glob:{f}"])
def test_read_ops_get_no_result_footer(tmp_path: Path, monkeypatch, arg: str) -> None:
    """A read op's own count line is already the last thing printed — nothing
    intervenes between it and end-of-output, so there is no lie to correct.
    A footer on every read would be pure noise on the highest-frequency path."""
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    f = tmp_path / "x.txt"
    f.write_text("alpha\n")
    assert "[result] " not in supertool.dispatch(arg.format(f=f)), arg


def test_replace_dry_gets_no_result_footer(tmp_path: Path, monkeypatch) -> None:
    """A preview writes nothing by design; `0 writes` would read as a failure."""
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    f = tmp_path / "x.txt"
    f.write_text("alpha\n")
    out = supertool.dispatch(f"replace_dry:::alpha:::gamma:::{f}")
    assert "[result] " not in out

# ---------------------------------------------------------------------------
# Declining instead of guessing
# ---------------------------------------------------------------------------

def test_result_line_declines_when_no_op_was_accounted_for() -> None:
    """`ops == 0` means the call never reached the mutation chokepoint — an
    argument-parse error, say. `0 ops run, 0 writes` would be a tidy invention;
    docs/validators.md's contract is to decline. So: no line at all."""
    assert supertool._result_line(0, 0) == ""
    assert supertool._result_line(-1, 0) == ""


def test_result_line_singularises_both_counts() -> None:
    assert supertool._result_line(1, 1) == "[result] 1 op run, 1 write\n"
    assert supertool._result_line(2, 2) == "[result] 2 ops run, 2 writes\n"
    assert supertool._result_line(1, 11) == "[result] 1 op run, 11 writes\n"
