"""#2360 -- census first, then the marker: how many test files here walk this
repository's own tree in a way whose answer cannot vary by OS or interpreter,
and does the `invariant` marker this file wires up actually reach them
without also reaching the three files #2360 named as tree-walking but
platform-sensitive.

Measured population, from `tests/_invariant_census.py` (recomputed here, not
copied, so a change to the census logic and a change to this pin cannot
silently drift apart): 92 tree-walking test files, 58 of them invariant by
the heuristic and 34 excluded as platform-sensitive, out of 1062 test files
total in this directory once this file itself exists -- 5.5% of the suite's
files. (An earlier count of 1061/59 total/invariant, quoted in this PR's own
commit message, was taken one commit before both this file and the
`os.pathsep` self-review fix below existed; a total counted from `tests/`
by a file that is itself a new addition to `tests/` is stale from the
instant it lands, and there is no fixed number to write here that survives
its own existence -- `_invariant_census.population()` is the number to
trust, always, never this docstring.)

Self-review found one gap in the heuristic before this landed:
`test_watch_sources_path_2135.py` walks the tree and was originally
classified invariant, despite its own docstring naming the exact defect it
guards -- a hardcoded `':'` splitting a Windows drive letter -- because
`_PLATFORM_SIGNAL` had no `os.pathsep` token. Fixed by adding one; this is
why the population is 58 rather than 59.

Timed locally (not on CI, and not a claim about CI's own wall clock): running
only the invariant population under `-n 4 --dist loadfile` (matching the
pytest job's `--dist loadfile`, at a worker count close to a hosted ubuntu
runner) is 1022 passed, 3 skipped in 31.20s wall, ~75.2s of summed per-test
duration -- against the 339.84s leg wall this issue's own tail sample was
measured against, that is a meaningfully larger slice than the top-25 tail's
own ~35s estimate, and above the 3% floor #2360 says would justify closing
without a deselect. Worth pursuing, not a halving: `--no-cov` already took
the other half of what `claude-oss` gained.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import _invariant_census

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent

# Named explicitly in #2360 as tree-walking guards whose *assertions* are
# still about platform behaviour: excluding them from 11 of 12 CI legs would
# silently narrow the coverage this matrix exists for. A "must not fire"
# assertion needs a positive control in the same fixture (this repo's own
# negative-assertion rule) -- see
# test_a_known_invariant_file_and_a_known_platform_sensitive_file_are_marked_apart
# below for the paired "must fire" half.
NAMED_PLATFORM_SENSITIVE = frozenset({
    "test_symlink_capability_1143.py",
    "test_git_shim_subcommand_1206.py",
    "test_no_bare_python3_spawn.py",
})

# A file the census's own `_walks_tree` classifies as invariant today, used
# as the "must fire" half of the paired assertion below. If this stops being
# true (the file stops walking the tree, or gains a platform signal), this
# module's own docstring numbers are stale and need re-deriving anyway.
_KNOWN_INVARIANT_FILE = "test_op_registry_1356.py"


def test_the_three_named_platform_sensitive_files_still_walk_the_tree_and_are_excluded():
    """The negative half: none of the three files #2360 named is ever marked
    `invariant`, however the census's internals change.
    """
    pop = _invariant_census.population()
    still_present = NAMED_PLATFORM_SENSITIVE & set(pop)
    assert still_present == NAMED_PLATFORM_SENSITIVE, (
        "one of the three files #2360 named no longer walks the tree by "
        "this census's own detector -- confirm deliberately before deleting "
        f"it from NAMED_PLATFORM_SENSITIVE: "
        f"{NAMED_PLATFORM_SENSITIVE - still_present}")
    invariant = _invariant_census.invariant_files()
    assert not (NAMED_PLATFORM_SENSITIVE & invariant), (
        "a file #2360 explicitly named as platform-sensitive despite "
        "walking the tree is about to be deselected on 11 of 12 CI legs: "
        f"{NAMED_PLATFORM_SENSITIVE & invariant}")


def test_a_known_invariant_file_is_still_classified_that_way():
    """The positive half, paired with the test above: a file this repo's own
    heuristic marks `invariant` must still land there, or the "must not
    fire" assertion above would be satisfied just as well by a census that
    marks nothing invariant at all -- the silence this repo's own
    negative-assertion rule exists to catch.
    """
    assert _KNOWN_INVARIANT_FILE in _invariant_census.invariant_files(), (
        f"{_KNOWN_INVARIANT_FILE} was invariant when this test was written; "
        "if the census logic changed, pick a new known-invariant example "
        "rather than deleting this half of the pair")


def test_invariant_marker_is_registered_in_pyproject():
    """An unregistered marker is not fatal here (`addopts` carries no
    `--strict-markers`), but leaving it unregistered means `pytest --markers`
    cannot describe it and a future `--strict-markers` sweep of this file
    would break on it silently later rather than declared here now.
    """
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"invariant:' in text, (
        "the invariant marker must be registered in pyproject.toml's "
        "[tool.pytest.ini_options] markers list")


def test_a_known_invariant_file_and_a_known_platform_sensitive_file_are_marked_apart():
    """End-to-end, not just unit-level: run pytest's own collector over one
    file from each side and check `pytest.mark.invariant` landed on exactly
    the one the census says it should. This is the test that would have
    stayed green if `tests/conftest.py` never wired the census into a
    collection hook at all -- the population module alone proves nothing
    about what pytest actually does with it.
    """
    platform_sensitive_file = "test_symlink_capability_1143.py"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", "invariant",
         "--no-cov", "-p", "no:cacheprovider",
         str(TESTS / _KNOWN_INVARIANT_FILE),
         str(TESTS / platform_sensitive_file)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
    )
    out = proc.stdout + proc.stderr
    assert _KNOWN_INVARIANT_FILE in out, (
        f"collecting with -m invariant found nothing from "
        f"{_KNOWN_INVARIANT_FILE}, which the census marks invariant:\n{out}")
    assert platform_sensitive_file not in out, (
        f"collecting with -m invariant picked up {platform_sensitive_file}, "
        f"one of the three files #2360 named as platform-sensitive:\n{out}")


def test_the_collection_hook_tolerates_a_synthetic_tree_missing_the_census(tmp_path):
    """The exact shape `test_git_env_leak_416.py` and `test_git_state_guard.py`
    already use: a synthetic, minimal repo tree that copies only
    `tests/conftest.py` -- not the rest of `tests/`, so `_invariant_census.py`
    is absent -- to test something unrelated in isolation. Before the
    conftest.py fix, `pytest_collection_modifyitems`'s unconditional `from
    _invariant_census import invariant_files` raised `ModuleNotFoundError`
    inside that subprocess and aborted its entire collection with an
    `INTERNALERROR`, failing five unrelated assertions in those two files
    about a synthetic run that never actually happened.
    """
    project = tmp_path / "synthetic"
    inner_tests = project / "tests"
    inner_tests.mkdir(parents=True)
    target = project / "supertool.py"
    try:
        target.symlink_to(ROOT / "supertool.py")
    except OSError:
        shutil.copy(ROOT / "supertool.py", target)
    shutil.copy(TESTS / "conftest.py", inner_tests / "conftest.py")
    (inner_tests / "test_inner.py").write_text(
        "def test_trivially_true():\n    assert True\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_inner.py", "-q",
         "--no-cov", "-p", "no:cacheprovider"],
        cwd=str(project), capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace",
    )
    out = proc.stdout + proc.stderr
    assert "INTERNALERROR" not in out, (
        f"the collection hook crashed a synthetic tree missing "
        f"_invariant_census.py instead of degrading to 'mark nothing':\n{out}")
    assert proc.returncode == 0 and "1 passed" in out, (
        f"synthetic tree's own trivial test did not pass cleanly:\n{out}")


def test_normal_collection_still_applies_the_marker_when_the_census_is_importable():
    """The other half of the same pair (#2360's own negative-assertion rule):
    the try/except added for the synthetic-tree case above must not silently
    swallow a real failure to apply the marker in the normal case, where
    `_invariant_census` genuinely is importable. Re-runs the existing
    end-to-end check to confirm the tolerant hook still marks a known file.
    """
    test_a_known_invariant_file_and_a_known_platform_sensitive_file_are_marked_apart()
