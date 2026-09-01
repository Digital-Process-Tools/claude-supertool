"""`tests/_isolated_child_tmp.py` (#2015, third hypothesis).

Six occurrences now on file. The first two named the suite's own
`XDG_CACHE_HOME` (`supertool-suite-cache-*`); the next two named a
git-fixture temp dir (`st756_*`); the sixth named `go-build3225713325` --
the Go toolchain's own build-cache directory, which this suite never
creates and does not own. The instrumentation this issue shipped in its
previous round proved the two directories we DID suspect (the system
tempdir and `XDG_CACHE_HOME`) were present and unchanged before and after
the nested child ran, so neither of them is what the child's collection
choked on.

The mechanism that explains all six: something reachable from the child's
own collection enumerates `%TEMP%` (or the equivalent on this platform)
and then touches an entry a DIFFERENT, unrelated process deletes in
between -- a listing-then-stat race, not a lifetime bug in any directory
this suite owns and not a spelling bug. The exact enumerator was not
pinned down from static reading of pytest 9.1.1, pytest-cov, pytest-xdist
and this repo's own code (all checked and found not to walk the raw
system tempdir in the child's actual collection path -- see the pull
request body for what was ruled out and why).

The mitigation that follows does not depend on identifying the
enumerator: give the child process its own PRIVATE `TMP`/`TEMP`/`TMPDIR`,
a freshly created, empty subdirectory of the parent test's own
`tmp_path`, so whatever ends up walking "the child's temp root" -- ours,
pytest's, or the OS's -- finds a directory with no unrelated program's
entries in it at all. A race needs two processes touching the same
directory; this removes the second one.
"""
from __future__ import annotations

import os

from _isolated_child_tmp import child_env_with_private_tmp


def test_tmp_temp_tmpdir_are_all_overridden_to_the_private_dir(tmp_path):
    private = tmp_path / "child-tmp"
    private.mkdir()
    base_env = {"TMP": "/somewhere/shared", "TEMP": "/somewhere/shared",
                "TMPDIR": "/somewhere/shared", "PATH": "/usr/bin"}

    env = child_env_with_private_tmp(base_env, private)

    assert env["TMP"] == str(private)
    assert env["TEMP"] == str(private)
    assert env["TMPDIR"] == str(private)


def test_unrelated_env_vars_are_preserved(tmp_path):
    """The must-fire half: this must not turn into wiping the child's env --
    only TMP/TEMP/TMPDIR move, everything else the child needs (PATH above
    all, or it cannot even start python.exe) survives untouched."""
    private = tmp_path / "child-tmp"
    private.mkdir()
    base_env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": "/some/lib"}

    env = child_env_with_private_tmp(base_env, private)

    assert env["PATH"] == "/usr/bin:/bin"
    assert env["PYTHONPATH"] == "/some/lib"


def test_the_base_env_mapping_is_not_mutated_in_place(tmp_path):
    private = tmp_path / "child-tmp"
    private.mkdir()
    base_env = {"TMP": "/somewhere/shared"}

    child_env_with_private_tmp(base_env, private)

    assert base_env["TMP"] == "/somewhere/shared", (
        "the caller's own env mapping (often os.environ itself) must not be "
        "silently rewritten as a side effect of building the child's env"
    )


def test_a_real_os_environ_snapshot_is_a_valid_base(tmp_path):
    """Must-fire against the real call shape used at both call sites:
    child_env_with_private_tmp(os.environ, ...), not a hand-built dict."""
    private = tmp_path / "child-tmp"
    private.mkdir()

    env = child_env_with_private_tmp(os.environ, private)

    assert env["TMP"] == str(private)
    assert env["TEMP"] == str(private)
    assert env["TMPDIR"] == str(private)
    # Something os.environ always carries survives through untouched.
    assert env.get("PATH") == os.environ.get("PATH")
