"""A `presets/watch` module must load in an interpreter nobody prepared (#1624).

`transport.py` inserts `presets/` on `sys.path` for `_proc` and then does a bare
`import naming` out of its *own* directory, which nothing puts there. It
resolved anyway whenever some other watch test module had already inserted it,
and `addopts` carries `-n auto`, so which worker got which file was a scheduling
accident. #1621 fixed the one test that tripped over it by inserting the
directory from the test file; that closes the instance and leaves the class —
the next caller to load one of these modules by path pays it again, and its
green is a fact about its neighbours rather than about the contract it asserts.

So the pin is on the modules, not on their callers: every module in
`presets/watch/` must import cleanly in a fresh interpreter whose `sys.path` was
prepared by nobody. A subprocess is the only honest way to ask — inside this
process the directory is already there, put by the imports above.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_WATCH = _ROOT / "presets" / "watch"

# Loads the module the same way every caller does — by path, through
# `spec_from_file_location` — and nothing else. Run with `-c`, so `sys.path[0]`
# is the *cwd*, which the test points at a tmp dir: a run from the repo root
# would not put `presets/watch` on the path either, but the tmp dir makes that
# independent of where pytest was invoked.
_PROBE = """
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("probe_target", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print("imported")
"""

# The whole tree, not just the top directory: `tiers/` and `sources/*/` load by
# path too and every one of them already imports standalone, so the wider
# population costs nothing today and is where the next instance of this shape
# would otherwise land unnoticed. Ids are the relative path — every source
# module is called `poller.py`, so bare names would collide into one case.
_MODULES = sorted(
    str(p.relative_to(_WATCH)) for p in _WATCH.rglob("*.py")
)


def test_the_module_list_is_not_empty() -> None:
    """The parametrisation below is derived from a glob.

    A glob that matches nothing turns every case below into zero cases, and a
    zero-case parametrisation is reported by pytest as a pass. That is this
    repository's house defect wearing a test runner, so the population is
    asserted non-empty before anything is claimed about its members.
    """
    assert _MODULES, f"no modules found under {_WATCH}"


@pytest.mark.parametrize("name", _MODULES)
def test_a_watch_module_imports_with_nobody_else_on_the_path(
    name: str, tmp_path: Path
) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(_WATCH / name)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"{name} does not import standalone:\n{proc.stderr}"
    )
