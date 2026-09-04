"""Shared verdict classifier for a nested-pytest child spawn (#2067, #2015).

`assert result.returncode == 0` renders a collection-time crash (exit 2: a
stale temp path, an import error) identically to a real product failure
(exit 1). That reading cost a log read, a filed issue and a CI re-run on
2026-08-28 (#2067) to establish that the env-var leak
`test_gl_repo_target_676.py` exists to catch was never in question -- the
child died before it could check.

Fixed once, locally, in `test_gl_repo_target_676.py`. Its GitHub-side twin,
`test_repo_target_673.py`, runs the identical probe technique and had not
been ported (#2015) -- so an occurrence there still misreported a harness
death as a product failure until this module was shared between the two.
"""
from __future__ import annotations

import subprocess
from typing import Callable, List, Tuple

import pytest

PYTEST_EXIT_MEANINGS = {
    2: "interrupted, or a collection error -- the child never finished "
       "collecting, let alone running, the tests it was asked about",
    3: "an internal pytest error",
    4: "a usage error in the child's own invocation",
    5: "no tests were collected",
}


def assert_child_pytest_ran_and_passed(
    result: subprocess.CompletedProcess, extra_detail: str = "",
) -> None:
    """Only exit code 1 is a verdict about the product under test.

    `extra_detail`, if given, is appended after the child's own stdout/stderr
    tail -- room for #2015's temp-state snapshots, so a harness failure
    carries the evidence needed to diagnose it instead of just the crash.
    """
    rc = result.returncode
    if rc == 0:
        return
    detail = result.stdout[-2000:] + result.stderr[-2000:]
    if extra_detail:
        detail = detail + "\n" + extra_detail
    if rc == 1:
        pytest.fail(f"the child pytest ran and disagreed (exit 1): {detail}")
    meaning = PYTEST_EXIT_MEANINGS.get(rc, f"an undocumented exit code {rc}")
    pytest.fail(
        f"the child pytest never produced a verdict -- exit {rc} "
        f"({meaning}), not a product failure:\n{detail}"
    )


def is_harness_death(result: subprocess.CompletedProcess) -> bool:
    """True when the child never produced a verdict about the product under
    test -- #2235.

    Exit 0 and exit 1 are real answers (pass, and a genuine product
    disagreement); everything else (2: a collection-time crash, 3/4/5:
    pytest's own internal/usage/no-tests states) means the child died before
    it could even try. This is the exact line `assert_child_pytest_ran_and_
    passed` above already draws between "ran and disagreed" and "never
    produced a verdict" -- pulled out here because `run_with_harness_retry`
    needs to ask the same question before the final assertion does.
    """
    return result.returncode not in (0, 1)


def run_with_harness_retry(
    spawn: Callable[[int], subprocess.CompletedProcess],
    max_attempts: int = 2,
) -> Tuple[subprocess.CompletedProcess, List[str]]:
    """Call `spawn(attempt)` (1-indexed) up to `max_attempts` times, retrying
    only a harness death -- #2235.

    #2235 is the seventh recorded occurrence of a nested pytest child dying
    at collection with a temp-directory `FileNotFoundError`, on Windows, with
    the private TMP/TEMP/TMPDIR redirect from #2015 already in place (that
    redirect narrows what THIS process touches; it cannot stop an unrelated
    program on the same runner -- antivirus, the Go toolchain, OS temp
    housekeeping -- from racing the shared system temp root while the child
    is starting up, which is outside this process's control). A rerun with
    nothing else changed passed clean, which is the profile of a harness
    hiccup, not a product regression.

    Retrying is safe here specifically because `is_harness_death` already
    tells `spawn`'s exit 1 (a real product disagreement) apart from every
    other exit code (the child never got far enough to answer at all): a
    retry can only ever recover from, or reconfirm, a run that never tested
    anything -- it can never paper over a result that was actually produced.
    Exit 1 is therefore never retried, bounded at `max_attempts` so a
    persistently broken child still fails loud rather than retrying forever.

    Returns the LAST result and one note per retried attempt, so the final
    assertion can show what happened on every attempt, not just the last.
    """
    notes: List[str] = []
    result = spawn(1)
    attempt = 1
    while is_harness_death(result) and attempt < max_attempts:
        notes.append(
            f"attempt {attempt}/{max_attempts}: harness death, exit "
            f"{result.returncode} -- retrying"
        )
        attempt += 1
        result = spawn(attempt)
    if is_harness_death(result) and notes:
        notes.append(
            f"attempt {attempt}/{max_attempts}: harness death, exit "
            f"{result.returncode} -- attempts exhausted"
        )
    return result, notes
