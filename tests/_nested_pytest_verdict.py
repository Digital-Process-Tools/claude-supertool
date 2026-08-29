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

import pytest

PYTEST_EXIT_MEANINGS = {
    2: "an internal pytest error during collection -- the child never finished "
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
