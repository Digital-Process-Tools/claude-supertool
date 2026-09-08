"""phplint must not read `php`'s own stdin fallback (#2412 follow-up).

`php -l --` looked fixed by the local verification #2412's original commit
recorded: a real installed php 8.2.0, given `-l -- -weird.php` with a CLOSED
local stdin, returned instantly with `No syntax errors detected`. What that
verification missed is in the message it printed and never checked: `No
syntax errors detected in Standard input code` -- `php -l` had ignored both
`--` and the filename after it, and fallen back to its own documented
behaviour of reading the script from stdin when no file argument is given.
A closed stdin delivers EOF immediately, so that fallback returned fast and
looked like a real, successful lint of the named file.

`validators/phplint/phplint.py`'s own `subprocess.run` call does not close
or redirect stdin, so it inherits whatever stdin the calling process has --
in this repo's CI, an open pipe from the harness that is never explicitly
closed. `php -l` then blocks reading that pipe until the adapter's own 30s
timeout, which is exactly what CI reported on PR #2412
(`tests/test_validators.py::test_phplint_adapter_valid_php` and
`..._broken_php_reports_line`, `[adapter] timeout` on every non-macOS leg).

This test reproduces the CI shape directly rather than trusting a quiet
local terminal. `Popen.communicate(input=None)` CLOSES stdin as its very
first step whenever stdin=PIPE -- CPython's own subprocess module does this
unconditionally (`self.stdin.close()` when `input` is falsy) -- so a naive
`Popen(..., stdin=PIPE); proc.communicate(timeout=...)` reproduces the
CLOSED-stdin case this bug's own false-positive local verification already
used, not the open-and-never-closed one CI actually hits. This is exactly
the trap the earlier version of this same test file fell into: it "passed"
on `phplint.py`'s unfixed `--` version in 2.46s, for the wrong reason.

So the stdin pipe here is built by hand with `os.pipe()`: the write end is
held open by the test process and NEVER closed and NEVER written to, which
is the actual shape a parent leaves a child's stdin when it does not
explicitly redirect it to DEVNULL. stdout/stderr are captured to real files
rather than PIPE, so draining them is never the thing gating this test's own
timeout -- only the adapter's own read of a stdin that will never deliver
EOF is.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ADAPTER = REPO / "validators" / "phplint" / "phplint.py"

needs_php = pytest.mark.skipif(shutil.which("php") is None, reason="php not installed")

#: Far short of the adapter's own 30s internal timeout -- the hang this test
#: guards against would blow straight through this and get killed below,
#: which is the failure signature to watch for if this test ever goes red.
BUDGET_S = 10


def _run_with_never_closed_stdin(cwd: Path, name: str):
    """Spawn the adapter with a stdin that stays open and silent throughout.

    Returns (elapsed_seconds, stdout_text, timed_out: bool). The pipe's
    write end is closed in the `finally`, after the wait/kill has already
    resolved -- closing it earlier would deliver EOF and defeat the whole
    point of this fixture.
    """
    read_fd, write_fd = os.pipe()
    out_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    err_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    start = time.time()
    proc = subprocess.Popen(
        [sys.executable, str(ADAPTER), name],
        cwd=str(cwd),
        stdin=read_fd,
        stdout=out_file,
        stderr=err_file,
    )
    os.close(read_fd)  # the child has its own copy; the parent's is unneeded
    timed_out = False
    try:
        proc.wait(timeout=BUDGET_S)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()
    finally:
        os.close(write_fd)  # never written to; only ever closed at the very end
    elapsed = time.time() - start
    out_file.seek(0)
    stdout = out_file.read()
    out_file.close()
    err_file.close()
    return elapsed, stdout, timed_out


@needs_php
def test_a_dash_named_target_does_not_hang_on_an_open_stdin(tmp_path: Path) -> None:
    name = "-hangcheck.php"
    (tmp_path / name).write_text("<?php echo 1;\n", encoding="utf-8")

    elapsed, stdout, timed_out = _run_with_never_closed_stdin(tmp_path, name)
    if timed_out:
        pytest.fail(
            "phplint.py did not answer within "
            f"{BUDGET_S}s against a flag-shaped filename with an open, "
            "never-closed stdin -- this is the exact CI hang #2412's "
            "original `--` fix reintroduced")
    assert elapsed < BUDGET_S, f"took {elapsed:.1f}s, budget was {BUDGET_S}s"

    out = json.loads(stdout.strip())
    assert out["tool"] == "phplint"
    assert out["file"] == name
    assert out["ok"] is True, out


@needs_php
def test_a_dash_named_broken_file_is_still_diagnosed_with_an_open_stdin(
        tmp_path: Path) -> None:
    """The positive control: containment must not swallow a real finding."""
    name = "-hangcheck-broken.php"
    (tmp_path / name).write_text("<?php echo 1\n", encoding="utf-8")  # missing ;

    elapsed, stdout, timed_out = _run_with_never_closed_stdin(tmp_path, name)
    if timed_out:
        pytest.fail("phplint.py hung on a broken, dash-named file too")

    out = json.loads(stdout.strip())
    assert out["ok"] is False, out
    assert out["count"] >= 1, out
