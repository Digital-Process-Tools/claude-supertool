"""#2372: the `invariant`-deselect step's own exclusion condition names two
matrix values by hardcoded literal (`ubuntu-latest`, `3.12`), and nothing
checked those literals against the matrix axes declared a few lines above
them in the same file.

`.github/workflows/tests.yml`'s pytest job runs the population `not
invariant` on every leg except the one meant to run everything --
`(matrix.os, matrix.python-version) == ("ubuntu-latest", "3.12")` -- via:

    if [ "$MATRIX_OS" != "ubuntu-latest" ] || [ "$MATRIX_PY" != "3.12" ]; then
      MARKER="$MARKER and not invariant"
    fi

The comment above that step (as of #2360) reads: "The default arm here (no
exclusion) is also the safe direction if MATRIX_OS/MATRIX_PY are ever wrong
or unset -- the worst case is every leg runs the invariant population, never
that a leg runs nothing." That gets the failure direction backwards on both
halves of its own claim:

* **Unset.** `MATRIX_OS`/`MATRIX_PY` unset means both compare against the
  empty string. `"" != "ubuntu-latest"` is true, so the `if` is true and the
  EXCLUSION branch runs -- not the "default arm (no exclusion)" the comment
  names. An unset variable narrows coverage; it does not widen it.
* **Repinned.** If `ubuntu-latest` is ever renamed in the matrix (say,
  `ubuntu-24.04`) or `3.12` drops out of `python-version`, then for EVERY
  leg -- including whichever leg was meant to be the one full run --
  `$MATRIX_OS != "ubuntu-latest"` is true, so the exclusion applies on all
  twelve legs and zero of them ever run the invariant population. That is
  the opposite of "every leg runs the invariant population": it is every
  leg deselecting it, silently, with no leg left to notice from.

Both failure modes are silent -- a deselected test reports nothing, so the
comment's own reassurance is exactly backwards about which direction is
actually safe. This file replaces the comment with an accurate one and adds
the check that was missing: the two literals in the `if` are asserted to be
members of the matrix's own declared `os:` and `python-version:` axes, so a
repin of either axis breaks this file loudly instead of leaving the
exclusion condition to quietly stop matching (or start matching everywhere)
on its own.
"""
from __future__ import annotations

import re

from _workflow_parse import job_blocks, job_steps, matrix_os, matrix_python_versions, run_blocks

#: The literal `!=` comparisons the exclusion step's `if` makes against the
#: two matrix-derived env vars it reads (`MATRIX_OS`, `MATRIX_PY`).
_OS_LITERAL_RE = re.compile(r'\[\s*"\$MATRIX_OS"\s*!=\s*"([^"]+)"\s*\]')
_PY_LITERAL_RE = re.compile(r'\[\s*"\$MATRIX_PY"\s*!=\s*"([^"]+)"\s*\]')


def _exclusion_run_block(pytest_block: str) -> str | None:
    """The one `run:` body in the pytest job that builds the `invariant`
    exclusion -- found by content (`not invariant`), not by step name or
    position, so a step reordering or rename cannot silently stop this file
    from finding it (the #557 shape `_workflow_parse.py` itself warns about).
    """
    for run in run_blocks(job_steps(pytest_block)):
        if "not invariant" in run and "MATRIX_OS" in run:
            return run
    return None


def _repin_literals(run_block: str) -> tuple[str | None, str | None]:
    os_match = _OS_LITERAL_RE.search(run_block)
    py_match = _PY_LITERAL_RE.search(run_block)
    return (
        os_match.group(1) if os_match else None,
        py_match.group(1) if py_match else None,
    )


def test_the_exclusion_step_is_still_found_in_the_real_workflow() -> None:
    """The must-fire half: if this stops matching anything, every assertion
    below would pass over zero real steps and report a clean sweep of
    nothing -- the exact shape this repo's own negative-assertion rule
    warns about."""
    block = job_blocks().get("pytest")
    assert block, "the pytest job block itself was not found"
    run = _exclusion_run_block(block)
    assert run is not None, (
        "no run: step in the pytest job builds an 'and not invariant' "
        "MARKER from MATRIX_OS/MATRIX_PY -- did the step get renamed, "
        "reworded, or removed?")
    os_literal, py_literal = _repin_literals(run)
    assert os_literal and py_literal, (
        f"found the exclusion step but could not extract both literal "
        f"comparisons out of it:\n{run}")


def test_the_exclusion_literals_are_still_members_of_the_declared_matrix() -> None:
    """The actual #2372 guard: the two hardcoded literals the `if` compares
    against must still name real entries in `matrix.os` and
    `matrix.python-version`, declared a few lines above in the same job
    block. If either axis is ever repinned (`ubuntu-latest` renamed, `3.12`
    dropped) without updating this `if`, the comparison silently stops
    matching correctly on every leg -- every leg decides it is NOT the
    designated full-population leg, and the whole matrix deselects
    `invariant` tests with no leg left to run them.
    """
    block = job_blocks()["pytest"]
    run = _exclusion_run_block(block)
    assert run is not None
    os_literal, py_literal = _repin_literals(run)

    declared_os = matrix_os(block)
    declared_py = matrix_python_versions(block)
    assert declared_os, "matrix.os could not be parsed out of the pytest job"
    assert declared_py, (
        "matrix.python-version could not be parsed out of the pytest job")

    assert os_literal in declared_os, (
        f"the exclusion step's if-condition compares MATRIX_OS against "
        f"{os_literal!r}, which is not in the declared matrix.os "
        f"{declared_os!r} -- every leg now treats itself as 'not the "
        f"designated full leg' and deselects invariant tests everywhere")
    assert py_literal in declared_py, (
        f"the exclusion step's if-condition compares MATRIX_PY against "
        f"{py_literal!r}, which is not in the declared matrix.python-version "
        f"{declared_py!r} -- every leg now treats itself as 'not the "
        f"designated full leg' and deselects invariant tests everywhere")


def test_a_repinned_axis_is_actually_caught_by_this_check() -> None:
    """The positive control paired with the test above (this repo's own
    negative-assertion rule): construct a synthetic job block where the
    matrix was repinned but the `if`'s literals were not updated, and
    confirm the membership check this file just made would have failed on
    it. Without this, `test_the_exclusion_literals_are_still_members_of_the_
    declared_matrix` passing on the real file could just as easily mean the
    parsing itself is silently broken and vacuously agreeing with anything.
    """
    stale_block = (
        "  pytest:\n"
        "    strategy:\n"
        "      matrix:\n"
        '        os: [ubuntu-24.04, macos-latest, windows-latest]\n'
        '        python-version: ["3.10", "3.11", "3.12", "3.13"]\n'
        "    steps:\n"
        "      - name: Run tests\n"
        "        run: |\n"
        '          MARKER="not slow and not benchmark"\n'
        '          if [ "$MATRIX_OS" != "ubuntu-latest" ] || [ "$MATRIX_PY" != "3.12" ]; then\n'
        '            MARKER="$MARKER and not invariant"\n'
        "          fi\n"
    )
    run = _exclusion_run_block(stale_block)
    assert run is not None, "the fixture's own step was not found by content"
    os_literal, py_literal = _repin_literals(run)
    declared_os = matrix_os(stale_block)
    declared_py = matrix_python_versions(stale_block)

    assert declared_os and declared_py, "the fixture's own matrix was not parsed"
    assert os_literal not in declared_os, (
        "the fixture repinned matrix.os away from 'ubuntu-latest' but this "
        "check still thinks the stale literal is a member -- it would not "
        "have caught the real #2372 failure mode")
