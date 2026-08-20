"""#572: the pre-push hook must not run the bare name `python3`.

`PYTHON="${PYTHON:-python3}"` is the same bet #529/#564 removed from the
Python side, left standing in the one place where losing it is hardest to
read. On Windows, PATH resolution of the bare name can hit the App Execution
Alias stub, which *blocks* rather than erroring — inside `git push` that
presents as a slow remote, not as a broken hook. On POSIX it loses more
quietly: it runs whatever `python3` PATH names that day rather than the
interpreter whose venv has the test dependencies, so the suite that gates the
push is not the suite the author has been running.

Per #570, only the bare name is aliased on Windows; `pythonX.Y` is not. So the
hook resolves a versioned interpreter, and — following the `$PYTHON39` ladder
in `supertool._syntax_floor_check` (docs/contributing.md: "the binary is asked
for its version; the filename is not believed") — it *executes* each candidate
before committing to it. `command -v` proves a name resolves, not that it
runs; a `python3.12` shim that is a stale symlink into a deleted venv answers
`command -v` and then fails at `-m pytest`, which is the silently-wrong
interpreter this issue is about, one level in.

The whole hook runs here, not an extracted snippet: the stubs on PATH answer
`-m pytest` too, so the run is instant and every assertion is about the real
file the maintainer's clone executes. Nothing touches the caller's PATH or
runs the real suite.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
HOOK = REPO / ".githooks" / "pre-push"
BASH = shutil.which("bash") or "/bin/bash"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason=".githooks/pre-push is a bash script"
)

# Newest first. The floor is pyproject's requires-python (3.9); the ceiling is
# a version ahead of the CI matrix so a developer on a new interpreter is not
# told to install an old one.
LADDER = ["python3.14", "python3.13", "python3.12", "python3.11", "python3.10", "python3.9"]

# `-c` is the hook's runnability probe; everything else is the suite run. The
# two exit codes are separate so a test can express "resolves, runs, and the
# suite is red" — which must still refuse the push — distinctly from "resolves
# and cannot run", which must move on to the next candidate.
_STUB = """#!/bin/bash
echo "{name} $*" >> "$STUB_LOG"
if [ "$1" = "-c" ]; then exit {probe}; fi
exit {suite}
"""


class _Sandbox:
    """A PATH with nothing on it but git and the interpreters a test names."""

    def __init__(self, tmp_path: Path) -> None:
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.log = tmp_path / "invocations.log"
        self.log.write_text("", encoding="utf-8")
        git = shutil.which("git")
        assert git is not None, "git is required to run the hook at all"
        os.symlink(git, self.bin / "git")
        subprocess.run(["git", "init", "-q", str(self.work)], check=True,
                       capture_output=True)

    def add_python(self, name: str, code: int = 0,
                   suite_code: int | None = None) -> Path:
        """A stub interpreter that logs every invocation.

        `code` is what makes execute-and-verify visible: a non-zero stub is a
        name that resolves and an interpreter that does not run. `suite_code`
        overrides the exit status of the suite run only.
        """
        path = self.bin / name
        path.write_text(
            _STUB.format(name=name, probe=code,
                         suite=code if suite_code is None else suite_code),
            encoding="utf-8")
        path.chmod(0o755)
        return path

    def add_venv(self, name: str = "venv", code: int = 0) -> Path:
        """An activated virtualenv, laid out the way `python -m venv` lays one
        out: a `bin/python3` that is a concrete file, not a PATH name."""
        venv = self.work / name
        (venv / "bin").mkdir(parents=True)
        stub = venv / "bin" / "python3"
        stub.write_text(
            _STUB.format(name=f"{name}/bin/python3", probe=code, suite=code),
            encoding="utf-8")
        stub.chmod(0o755)
        return venv

    def run(self, python: str | None = None,
            virtual_env: str | None = None) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": str(self.bin),
            "HOME": str(self.work),
            "STUB_LOG": str(self.log),
            # This file is about interpreter resolution, not about which pushes
            # are gated, and it invokes the hook with no argv and no stdin. That
            # used to reach the suite through the not-git fallback arm, which
            # #1802 retired — an incidental dependency that made eight tests here
            # fail on a change to a question they do not ask. `PREPUSH_FULL=1` is
            # the supported "run it anyway" and states the dependency instead of
            # inheriting it from whatever the zero-ref default happens to be.
            "PREPUSH_FULL": "1",
        }
        if python is not None:
            env["PYTHON"] = python
        if virtual_env is not None:
            env["VIRTUAL_ENV"] = virtual_env
        return subprocess.run(
            [BASH, str(HOOK)], cwd=str(self.work), env=env,
            capture_output=True, encoding="utf-8", errors="replace", timeout=60,
        )

    def invocations(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def ran_the_suite(self) -> list[str]:
        """Which interpreters were asked to run the suite, in order."""
        return [line.split()[0] for line in self.invocations() if "-m pytest" in line]


@pytest.fixture
def box(tmp_path: Path) -> _Sandbox:
    return _Sandbox(tmp_path)


# ---------------------------------------------------------------------------
# failure is loud, and names what it tried
# ---------------------------------------------------------------------------

def test_no_versioned_interpreter_is_a_refused_push(box: _Sandbox) -> None:
    r = box.run()
    assert r.returncode != 0
    assert box.ran_the_suite() == []


def test_the_refusal_names_every_interpreter_it_tried(box: _Sandbox) -> None:
    """"python: command not found" sends the author looking at their PATH.
    The names are the fix instruction."""
    r = box.run()
    out = r.stdout + r.stderr
    for name in LADDER:
        assert name in out, f"the refusal does not mention {name}"
    assert "PYTHON" in out, "the refusal does not mention the override"


def test_a_bare_python3_on_path_is_not_a_fallback(box: _Sandbox) -> None:
    """The whole point. A bare `python3` exists and is runnable, and the hook
    must still refuse rather than execute it — that name is the one that can
    block forever on Windows."""
    box.add_python("python3")
    r = box.run()
    assert r.returncode != 0
    assert box.invocations() == []


# ---------------------------------------------------------------------------
# the normal case, and the order
# ---------------------------------------------------------------------------

def test_a_versioned_interpreter_runs_the_suite(box: _Sandbox) -> None:
    box.add_python("python3.11")
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert box.ran_the_suite() == ["python3.11"]
    assert any("-m pytest" in line and "--no-cov" in line and "benchmark" in line
               for line in box.invocations())


def test_the_newest_interpreter_wins(box: _Sandbox) -> None:
    box.add_python("python3.9")
    box.add_python("python3.12")
    box.add_python("python3.10")
    box.run()
    assert box.ran_the_suite() == ["python3.12"]


def test_the_hook_still_reports_a_failing_suite(box: _Sandbox) -> None:
    """Resolution is new; the gate is not. A resolved, runnable interpreter
    reporting a red suite must still refuse the push, with the hook's own
    message rather than the resolution failure."""
    box.add_python("python3.12", suite_code=1)
    r = box.run()
    assert r.returncode != 0
    assert box.ran_the_suite() == ["python3.12"]
    assert "Push aborted" in r.stdout


# ---------------------------------------------------------------------------
# resolved means "it ran", not "the name is on PATH"
# ---------------------------------------------------------------------------

def test_a_name_that_resolves_but_does_not_run_is_skipped(box: _Sandbox) -> None:
    """A stale `python3.13` symlink into a deleted venv answers `command -v`
    and cannot execute. Believing the name here substitutes a broken
    interpreter for a working one and reports it as a test failure."""
    box.add_python("python3.13", code=127)
    box.add_python("python3.11")
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert box.ran_the_suite() == ["python3.11"]


def test_every_candidate_unrunnable_is_still_a_loud_refusal(box: _Sandbox) -> None:
    box.add_python("python3.12", code=127)
    box.add_python("python3.10", code=127)
    r = box.run()
    assert r.returncode != 0
    assert box.ran_the_suite() == []


# ---------------------------------------------------------------------------
# an activated venv is the interpreter the author has been running
# ---------------------------------------------------------------------------

def test_an_activated_venv_beats_a_newer_versioned_name(box: _Sandbox) -> None:
    """The POSIX half of #572, which newest-first would otherwise recreate: a
    system python3.13 next to a 3.11 venv is exactly the "different set of
    installed packages" swap the bare name was doing."""
    venv = box.add_venv()
    box.add_python("python3.13")
    r = box.run(virtual_env=str(venv))
    assert r.returncode == 0, r.stdout + r.stderr
    assert box.ran_the_suite() == ["venv/bin/python3"]


def test_a_broken_venv_falls_through_to_the_ladder(box: _Sandbox) -> None:
    venv = box.add_venv(code=127)
    box.add_python("python3.12")
    r = box.run(virtual_env=str(venv))
    assert r.returncode == 0, r.stdout + r.stderr
    assert box.ran_the_suite() == ["python3.12"]


def test_an_explicit_python_beats_an_activated_venv(box: _Sandbox) -> None:
    venv = box.add_venv()
    mine = box.add_python("my-venv-python")
    box.run(python=str(mine), virtual_env=str(venv))
    assert box.ran_the_suite() == ["my-venv-python"]


# ---------------------------------------------------------------------------
# the override is untouched
# ---------------------------------------------------------------------------

def test_an_explicit_python_is_used_verbatim(box: _Sandbox) -> None:
    """PYTHON= is how a developer points the hook at one specific venv. No
    resolution may second-guess it, including when nothing else resolves."""
    mine = box.add_python("my-venv-python")
    r = box.run(python=str(mine))
    assert r.returncode == 0, r.stdout + r.stderr
    assert box.ran_the_suite() == ["my-venv-python"]


def test_an_explicit_python_beats_a_versioned_one_on_path(box: _Sandbox) -> None:
    mine = box.add_python("my-venv-python")
    box.add_python("python3.12")
    box.run(python=str(mine))
    assert box.ran_the_suite() == ["my-venv-python"]


def test_an_explicit_python_is_not_verified_away(box: _Sandbox) -> None:
    """An override that fails is the author's own choice and their own error
    message. Silently replacing it with something that works would hide which
    interpreter the suite actually ran under."""
    broken = box.add_python("my-venv-python", code=1)
    box.add_python("python3.12")
    r = box.run(python=str(broken))
    assert r.returncode != 0
    assert box.ran_the_suite() == ["my-venv-python"]
