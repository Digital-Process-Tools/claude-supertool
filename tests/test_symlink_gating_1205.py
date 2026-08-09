"""#1205 -- symlink tests that fail where their siblings skip.

`tests/_symlink.py` (#1143) exists so the suite takes the "can I make a symlink
here" decision once, by probing, and reports the answer as a skip carrying a
stated reason. A test that creates a symlink without asking it does something
different on a runner without the privilege: `symlink_to` raises `OSError` and
the test **fails**.

A skip and a failure carry opposite meanings to whoever reads the leg. A skip
says the platform could not run the check. A failure says the platform ran it
and the code is broken -- pointing, here, at path-meta code that is fine. That
is this repo's own defect class landing in the harness: an environmental
inability rendered as a product verdict.

This runs `test_path_meta_bulk_1126.py`'s symlink tests in a subprocess with the
privilege forced absent, which is the only way to observe the difference from a
machine that has it. Not `skipif(os.name == "nt")` on the assertion -- that
would be the vacuous-on-one-platform branch #1143 was written to remove.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent

#: The privilege is refused for the child process only, in the two places a test
#: can reach it. `_symlink._PROBE` is pinned as well: the probe would otherwise
#: succeed against the patched-out `os.symlink` and report a capability the
#: child no longer has.
_PLUGIN = """
import errno
import os
import pathlib
import sys


def pytest_configure(config):
    sys.path.insert(0, {tests!r})
    import _symlink
    _symlink._PROBE = (False, "forced absent by the #1205 guard")

    def _refuse(*a, **k):
        raise OSError(errno.EPERM, "symlink creation refused (#1205 guard)")

    os.symlink = _refuse
    pathlib.Path.symlink_to = _refuse
"""


@pytest.fixture(scope="module")
def without_the_privilege(tmp_path_factory) -> subprocess.CompletedProcess:
    d = tmp_path_factory.mktemp("nosymlink")
    (d / "nosymlinkplugin.py").write_text(
        _PLUGIN.format(tests=str(TESTS)), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(d),
               PYTEST_ADDOPTS="", PYTEST_DISABLE_PLUGIN_AUTOLOAD="")
    return subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_path_meta_bulk_1126.py", "-k", "symlink", "-rs",
         "-p", "nosymlinkplugin", "-p", "no:cacheprovider", "-p", "no:xdist",
         "-p", "no:cov", "-q", "--no-header", "-o", "addopts="],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300,
        encoding="utf-8", errors="replace", env=env)


def test_every_symlink_test_in_the_bulk_file_skips_rather_than_fails(
    without_the_privilege
) -> None:
    """The whole point: no failures, no errors, on a runner that cannot link."""
    r = without_the_privilege
    assert r.returncode == 0, (
        "with the create-symlink privilege absent, the symlink tests in "
        "test_path_meta_bulk_1126.py did not all skip -- an environment limit "
        "is being reported as a defect in the code under test:"
        + os.linesep + r.stdout + os.linesep + r.stderr
    )
    assert " failed" not in r.stdout and " error" not in r.stdout, r.stdout


def test_the_run_actually_exercised_the_tests_it_claims_to_have_skipped(
    without_the_privilege
) -> None:
    """A green from a run that collected nothing would prove nothing.

    Deselection, a renamed test or a typo in `-k` all produce `0 passed` and
    exit 0, which reads exactly like the skips this file exists to require.
    """
    r = without_the_privilege
    assert "3 skipped" in r.stdout, (
        "expected the three symlink-creating tests of "
        "test_path_meta_bulk_1126.py to be collected and skipped: "
        + os.linesep + r.stdout + os.linesep + r.stderr
    )


def test_the_skips_name_the_shared_reason_rather_than_a_local_wording(
    without_the_privilege
) -> None:
    """A skip nobody can grep for is not a legible blind spot (#1143)."""
    import _symlink

    assert _symlink.TOKEN in without_the_privilege.stdout, (
        "the skips did not carry " + _symlink.TOKEN + ", so they are "
        "indistinguishable from the ~680 others in a Windows leg:"
        + os.linesep + without_the_privilege.stdout
    )
