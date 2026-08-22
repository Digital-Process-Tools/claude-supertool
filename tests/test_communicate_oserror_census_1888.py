"""Census (#1888): does any other validators/* adapter treat an `OSError`

from `communicate()` as proof the child is dead?

PR #1883 fixed exactly that shape in `validators/git-status/git-status.py`:
a broken pipe under a *live* child (`OSError`) is not the same event as a
*dead* child, and the first `_stop()` conflated them, leaking a process that
could still be holding `.git/index.lock`. This file is the swept population
that question was asked over, not a diff for its own sake — #1846 and #1864
both filed counts against this repository that undercounted the real
population (~17 filed against 23 and 32 actual), so the population here is
derived from the tree on every run rather than typed in from a prior read.

**Result: git-status.py is the only adapter under `validators/` that spawns
via `subprocess.Popen` and owns a manual `communicate()`/kill teardown path.**
Every other adapter that spawns a subprocess at all does so through a single
`subprocess.run(..., timeout=...)` call — never its own `Popen` object, never
its own `.communicate()`, never its own kill escalation. There is no site in
any of them where an adapter's *own* code reads an `OSError` from
`communicate()` and decides the child is gone; that decision belongs entirely
to the CPython stdlib's internal handling of `subprocess.run(timeout=)`,
which is identical across every call site and out of any one adapter's
control. So the population outside git-status.py is `does not have the
shape` across the board, not `cannot tell` — the shape (an adapter's own
teardown path making that call) is simply absent.

**A third state was still findable, and it turned up inside the reference
file itself.** `git-status.py`'s outer `run()` wraps its *first*
`proc.communicate(timeout=remaining)` call (not the one inside `_settled`,
which is only reached after a `TimeoutExpired` has already fired) in a `try`
that catches `subprocess.TimeoutExpired` alone — not `OSError`. Measured
against a real child on this platform:

    p = subprocess.Popen(["sleep", "5"], stdout=PIPE, stderr=PIPE)
    os.close(p.stdout.fileno())
    p.communicate(timeout=1)   # raises OSError(9, "Bad file descriptor")
    p.poll() is None           # True -- the child is alive

so the same broken-pipe-not-dead-child event the whole file exists to handle
can still reach `run()` directly, bypass `_stop()` entirely (it is only
called from the `except TimeoutExpired` arm), and propagate uncaught out of
`main()` — leaking the child instead of stopping it. `git-status/` is held
by the #1888 brief as the reference implementation, not a site to fix in
this diff, so that gap is filed rather than patched here (see the #1888
report). This test does not re-litigate it; `test_only_git_status_spawns_
via_popen_with_its_own_teardown` below only pins that git-status.py is still
the one file with the *shape*, so a second adapter growing its own `Popen`
teardown does not pass silently as "does not have the shape" the next time
this census is read.
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_VALIDATORS = _ROOT / "validators"

#: The one file this census found with its own Popen + communicate() +
#: kill/terminate teardown path (#1883). Any other file matching that shape
#: is new population this census has not looked at.
_OWNS_MANUAL_TEARDOWN = {"git-status/git-status.py"}

#: Matches the bare call, not the qualified spelling, so `from subprocess
#: import Popen` and `import subprocess as sp; sp.Popen(` are seen too --
#: a regex tied to `subprocess.Popen(` alone missed both (found in review).
_POPEN_RE = re.compile(r"\bPopen\s*\(")
_SUBPROCESS_IMPORT_RE = re.compile(
    r"^\s*(import subprocess\b|from subprocess import\b)", re.MULTILINE
)


def _validator_py_files() -> list[Path]:
    return sorted(_VALIDATORS.rglob("*.py"))


def test_the_census_saw_every_validator_file():
    """A population derived by hand is exactly what #1846 and #1864 got

    wrong. Assert the sweep is non-trivial so an empty glob cannot pass this
    file by finding nothing to check.
    """
    files = _validator_py_files()
    assert len(files) >= 30, (
        "validators/**/*.py returned suspiciously few files ("
        + str(len(files)) + ") -- the census may be looking at the wrong "
        "tree rather than at a genuinely small population"
    )


def test_only_git_status_spawns_via_popen_with_its_own_teardown():
    """Every other adapter hands teardown to `subprocess.run`'s own timeout

    handling rather than writing its own `Popen`/`communicate()`/kill path --
    so there is no *adapter* code, outside git-status.py, that could read an
    `OSError` from `communicate()` as "the child is dead". A new adapter
    growing a manual `Popen` teardown is exactly the shape #1888 asked the
    census to watch for, so it must be added to `_OWNS_MANUAL_TEARDOWN` (and
    audited for this defect) rather than pass here unexamined.
    """
    unexpected = []
    for path in _validator_py_files():
        rel = str(path.relative_to(_VALIDATORS)).replace("\\", "/")
        text = path.read_text(encoding="utf-8")
        has_popen = bool(_POPEN_RE.search(text))
        if rel in _OWNS_MANUAL_TEARDOWN:
            assert has_popen, (
                rel + " is recorded as owning a manual Popen teardown path "
                "but no longer calls subprocess.Popen -- the census "
                "assumption is stale, update _OWNS_MANUAL_TEARDOWN"
            )
            continue
        if has_popen:
            unexpected.append(rel)
    assert not unexpected, (
        "adapter(s) now spawn via subprocess.Popen with what looks like "
        "their own teardown path, uncovered by the #1888 census: "
        + ", ".join(unexpected) + " -- audit each for the OSError-from-"
        "communicate()-read-as-dead-child shape PR #1883 fixed in "
        "git-status.py, then add it to _OWNS_MANUAL_TEARDOWN above"
    )


def test_every_other_spawning_adapter_uses_run_not_popen():
    """The positive control for the test above: an adapter that spawns at

    all, and is not git-status.py, must do so through `subprocess.run` --
    proving the census is not merely failing to find any spawn at all in
    those files (which would make the assertion above vacuous).
    """
    spawns_via_run = []
    for path in _validator_py_files():
        rel = str(path.relative_to(_VALIDATORS)).replace("\\", "/")
        if rel in _OWNS_MANUAL_TEARDOWN:
            continue
        text = path.read_text(encoding="utf-8")
        if _SUBPROCESS_IMPORT_RE.search(text):
            assert "subprocess.run(" in text, (
                rel + " imports subprocess but calls neither .run( nor "
                ".Popen( on it -- the census could not classify this file, "
                "which is not the same as it being clean"
            )
            spawns_via_run.append(rel)
    assert len(spawns_via_run) >= 20, (
        "expected at least 20 adapters spawning via subprocess.run alone; "
        "found " + str(len(spawns_via_run)) + " -- either the population "
        "shrank or this test stopped seeing it: " + str(spawns_via_run)
    )
