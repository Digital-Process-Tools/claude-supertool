"""#2019: the #2012 markdownlint ceiling is unenforced in CI.

No leg of `.github/workflows/tests.yml` installs `markdownlint-cli`, so the
four measurement tests in `tests/test_markdownlint_noise_2012.py`
(`test_repo_wide_markdownlint_findings_stay_under_the_2012_ceiling`,
`test_markdownlint_positive_control_a_real_finding_is_still_caught`,
`test_known_residue_files_are_the_only_ones_left_with_findings`, and the two
config-shape tests that do not need the binary at all) skip on every one of
the twelve pytest legs plus the coverage leg -- and a skip renders identically
to a pass on any board that counts red legs.

This guard reads the workflow's structure (`_workflow_parse`), not its text,
for the reason that module's own docstring gives: `test_ci_non_python_coverage_557.py`
and `test_ci_job_timeouts_722.py` were both burned by a `X in text` needle
satisfied by a comment describing X rather than a step doing X.
"""
from __future__ import annotations

from _workflow_parse import job_blocks, job_steps, run_blocks, workflow_text


def _all_run_blocks() -> list[str]:
    text = workflow_text()
    blocks = job_blocks(text)
    assert blocks, "job_blocks() found no jobs at all -- parser regression, not a clean workflow"
    out: list[str] = []
    for block in blocks.values():
        out.extend(run_blocks(job_steps(block)))
    return out


def test_positive_control_pip_install_is_found_by_this_parser() -> None:
    """Proves the walk below can see a real install line at all -- a guard
    that never finds `pip install` would also never find `markdownlint-cli`,
    and the two failures read identically without this control."""
    runs = _all_run_blocks()
    assert any("pip install" in r for r in runs), (
        "no run: block anywhere in tests.yml contains 'pip install' -- "
        "the parser is not seeing real step bodies, so its silence about "
        "markdownlint-cli below is not evidence of anything"
    )


def test_some_job_installs_markdownlint_cli() -> None:
    runs = _all_run_blocks()
    assert any("markdownlint-cli" in r for r in runs), (
        "no job in .github/workflows/tests.yml installs markdownlint-cli -- "
        "the four measurement tests in tests/test_markdownlint_noise_2012.py "
        "skip on every CI leg, and a skip renders as a pass on any board that "
        "counts red legs (#2019). Add an `npm install -g markdownlint-cli@<pinned>` "
        "step to a job that also runs `pytest` over tests/, before the test step."
    )


def test_the_markdownlint_install_precedes_the_test_run_in_its_own_job() -> None:
    """Installing it in a job that never runs pytest, or after the test step
    in one that does, would satisfy the substring check above while leaving
    every measurement test still skipping."""
    blocks = job_blocks(workflow_text())
    for name, block in blocks.items():
        steps = job_steps(block)
        install_idx = next(
            (i for i, s in enumerate(steps) if "markdownlint-cli" in s.run), None
        )
        if install_idx is None:
            continue
        # A job may run pytest directly (`run: ... -m pytest ...`, the
        # twelve-leg matrix) or through a wrapper script that itself invokes
        # pytest (the coverage job's `coverage_gate.py`, per its own
        # `subprocess.run([..., "-m", "pytest", ...])`) -- either way the
        # measurement tests only execute if a pytest run happens downstream
        # of the install, in this same job.
        test_idx = next(
            (
                i for i, s in enumerate(steps)
                if "pytest" in s.run or "coverage_gate.py" in s.run
            ),
            None,
        )
        assert test_idx is not None, (
            f"job {name!r} installs markdownlint-cli but never runs pytest "
            "(directly or via a wrapper script) in the same job -- the "
            "install buys nothing there"
        )
        assert install_idx < test_idx, (
            f"job {name!r} installs markdownlint-cli (step {install_idx}) "
            f"after it already ran pytest (step {test_idx}) -- the "
            "measurement tests will have already skipped"
        )
        return
    raise AssertionError("no job installs markdownlint-cli at all")
