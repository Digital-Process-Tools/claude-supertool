"""Pins the `tests.yml` step that provisions `awk` for the windows-latest leg
(#2505), through the structural workflow parser (`tests/_workflow_parse.py`)
rather than a text needle -- `tests.yml` is mostly comment explaining its own
decisions, and a needle over raw text can be satisfied by the prose describing
a step rather than by the step itself (see `_workflow_parse.py`'s own
docstring, #731).

Would this pass if the step were deleted, or if it existed but its `run:`
body no longer checked `awk`'s presence? No -- `job_steps` would either omit
the step from the list, or return it with a `run` body that fails the
`in` checks below.
"""
from __future__ import annotations

from _workflow_parse import job_blocks, job_steps

STEP_NAME = "Ensure awk is on PATH (Windows) (#2505)"


def _pytest_job_steps():
    blocks = job_blocks()
    assert "pytest" in blocks, "no 'pytest' job in tests.yml"
    steps = job_steps(blocks["pytest"])
    assert steps, "no steps parsed out of the pytest job"
    return steps


def test_the_awk_provisioning_step_exists():
    names = [step.name for step in _pytest_job_steps()]
    assert STEP_NAME in names, (
        "the windows-only awk-provisioning step is missing from tests.yml's "
        "pytest job (#2505); names seen: {0}".format(names))


def test_the_step_checks_for_awk_and_fails_loudly_if_still_absent():
    steps = _pytest_job_steps()
    step = next(s for s in steps if s.name == STEP_NAME)
    assert "command -v awk" in step.run
    assert "exit 1" in step.run, (
        "the step must fail the job when awk is still missing after "
        "provisioning, not degrade quietly (#2505)")


def test_the_step_runs_before_the_test_step():
    """Provisioning that lands after `Run tests` has already run protects
    nothing -- pin the ordering, not just the presence."""
    names = [step.name for step in _pytest_job_steps()]
    provision_at = names.index(STEP_NAME)
    test_at = next(i for i, n in enumerate(names)
                   if n.startswith("Run tests"))
    assert provision_at < test_at


def test_the_repair_body_names_the_real_git_for_windows_path():
    """`command -v awk` and `exit 1` alone only pin that the step CHECKS for
    awk and fails loudly if it is absent -- not that the repair path it tries
    first (adding Git's own `usr/bin`, where `awk.exe` ships) is spelled
    correctly. A typo in either literal would not turn `test_the_step_checks_
    for_awk_and_fails_loudly_if_still_absent` red, because that test never
    reads these two lines -- caught in self-review of #2505, not assumed
    covered by the two checks above.

    Would this pass if the windows-style path written to `GITHUB_PATH` no
    longer matched the MSYS-style path exported into the current shell's own
    `PATH`? No -- `GITHUB_PATH` receives Windows steps' PATH (backslash
    form), the `export PATH=` line affects only the current `shell: bash`
    process (MSYS `/c/...` form); the two are deliberately different
    spellings of the same directory, pinned as a pair here rather than
    separately, because the whole point of the fallback is that both take
    effect."""
    steps = _pytest_job_steps()
    step = next(s for s in steps if s.name == STEP_NAME)
    assert r"C:\Program Files\Git\usr\bin" in step.run, (
        "the GITHUB_PATH line's Windows-style path is missing or misspelled")
    assert "/c/Program Files/Git/usr/bin" in step.run, (
        "the export PATH= line's MSYS-style path is missing or misspelled")
