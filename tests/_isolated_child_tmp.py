"""Give a nested-pytest child its own private temp root (#2015, third round).

Six occurrences of a nested pytest child dying during collection are on
file. The instrumentation shipped in this issue's previous round (see
`tests/_child_temp_diagnostics.py`) proved on its first real firing that
the two directories anyone had suspected -- the system tempdir and this
suite's own `XDG_CACHE_HOME` -- were present and byte-identical before
and after the child ran. The sixth occurrence named `go-build3225713325`,
the Go toolchain's own build-cache directory: not ours, not pytest's own
retention machinery (which is lazy and never triggers for these two
narrow nested calls, since neither the target test nor the probe test
requests `tmp_path`), and not reachable from anything this repository's
own code does with `tempfile.gettempdir()` (checked: no call site under
`tests/`, `presets/`, or `_supertool.py` enumerates the raw system
tempdir; each one either joins a fixed leaf name onto it or reads/writes
one file there).

What ties all six together is not which directory failed, but that
*something* enumerates the process's own temp root during the child's
collection and then touches an entry a different, unrelated program
deletes in between -- a listing-then-stat race. The exact enumerator was
not pinned down without Windows instrumentation (a debugger or Process
Monitor trace this session cannot run), so this does not claim to have
found it.

The mitigation below does not need that identity. A race needs two
processes touching the same directory; `child_env_with_private_tmp` gives
the nested child a `TMP`/`TEMP`/`TMPDIR` that no other process on the
machine has ever heard of -- a fresh, empty subdirectory of the calling
test's own `tmp_path` -- so whatever ends up walking "the child's temp
root", ours or pytest's or the OS's, finds nothing belonging to `go
build`, `git`, or anything else running on the same runner.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


def child_env_with_private_tmp(
    base_env: Mapping[str, str], private_dir: Path,
) -> dict[str, str]:
    """A copy of `base_env` with TMP/TEMP/TMPDIR redirected to `private_dir`.

    `private_dir` must already exist and be empty -- creating it is the
    caller's job (normally a subdirectory of its own `tmp_path`), because
    only the caller knows what it wants that directory named and whether
    it should survive the test's own teardown.

    Every other variable in `base_env` -- PATH above all, without which
    the child's `python.exe`/`python3` cannot even start -- passes through
    untouched. `base_env` is never mutated: the common call shape is
    `child_env_with_private_tmp(os.environ, ...)`, and os.environ is
    process-global state nothing here should be rewriting as a side
    effect.
    """
    env = dict(base_env)
    private = str(private_dir)
    env["TMP"] = private
    env["TEMP"] = private
    env["TMPDIR"] = private
    return env
